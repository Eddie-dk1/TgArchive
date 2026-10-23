"""Durable history, media queue, run history and local export formats."""
import bisect
import csv
import fcntl
import html
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from telethon import utils

from .common import PAGE_SIZE, atomic_text, classify_message, dumps, expected_size, now


class Archive:
    def __init__(self, root, entity, account_id):
        self.chat_id = utils.get_peer_id(entity)
        self.root = Path(root).expanduser().resolve() / f'channel_{self.chat_id}'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (self.root / '.archive.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError('Этот архив уже обрабатывается другим окном приложения.') from None
        self.db = sqlite3.connect(self.root / 'archive.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.run_id = None
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, date TEXT, text TEXT, category TEXT,
                    media_key TEXT, path TEXT, size INTEGER, status TEXT,
                    error TEXT, link TEXT, raw TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
            ''')
            owner = self.get_meta('account_id')
            if owner is not None and owner != account_id:
                raise ValueError('Этот архив создан другим аккаунтом. Выберите другую папку.')
            columns = {r['name'] for r in self.db.execute('PRAGMA table_info(messages)')}
            previous_version = self.get_meta('schema_version', 1)
            migration = previous_version < 2
            with self.db:
                for name, definition in (
                    ('expected_size', 'INTEGER'), ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                    ('availability', "TEXT NOT NULL DEFAULT 'available'"), ('seen_scan', 'TEXT'),
                    ('sha256', 'TEXT'), ('verified_at', 'TEXT'),
                ):
                    if name not in columns:
                        self.db.execute(f'ALTER TABLE messages ADD COLUMN {name} {definition}')
                self.db.executescript('''
                    CREATE TABLE IF NOT EXISTS runs (
                        id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT,
                        mode TEXT, outcome TEXT, bytes_transferred INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, time TEXT, run_id INTEGER, message_id INTEGER,
                        phase TEXT, level TEXT, event TEXT, detail TEXT, attempt INTEGER,
                        delay_seconds REAL
                    );
                    CREATE TABLE IF NOT EXISTS gap_checks (
                        message_id INTEGER PRIMARY KEY, scan_id TEXT, status TEXT, checked_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS queue_status ON messages(status, availability, id);
                    CREATE INDEX IF NOT EXISTS scan_seen ON messages(seen_scan, id);
                ''')
                if migration:
                    for row in self.db.execute('SELECT id,raw FROM messages'):
                        self.db.execute('UPDATE messages SET expected_size=? WHERE id=?',
                                        (expected_size(json.loads(row['raw'])), row['id']))
                self.db.execute("UPDATE messages SET status='pending' WHERE status='downloading'")
            self.set_meta('account_id', account_id)
            self.set_meta('schema_version', 3)
            self.state = self.get_meta('scan_state')
            self.info = {'chat_id': self.chat_id, 'title': entity.title,
                         'username': getattr(entity, 'username', None), 'account_id': account_id,
                         'content_protected': bool(getattr(entity, 'noforwards', False)),
                         'schema_version': 3,
                         'scope': 'Channel posts visible to this account; discussion replies are separate.'}
            if previous_version < 3:
                self.log('schema_migrated', phase='prepare', detail='SQLite schema upgraded to version 3')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.db.close()
        self.lock.close()

    def get_meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, dumps(value)))

    def save_state(self):
        self.set_meta('scan_state', self.state)

    def log(self, event, *, message_id=None, phase='media', level='info', detail='', attempt=None, delay=None):
        record = dict(time=now(), run_id=self.run_id, message_id=message_id, phase=phase,
                      level=level, event=event, detail=detail, attempt=attempt, delay_seconds=delay)
        with self.db:
            cursor = self.db.execute('''INSERT INTO events
                (time,run_id,message_id,phase,level,event,detail,attempt,delay_seconds)
                VALUES (:time,:run_id,:message_id,:phase,:level,:event,:detail,:attempt,:delay_seconds)''', record)
        record['id'] = cursor.lastrowid
        with (self.root / 'journal.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(dumps(record) + '\n')

    def start_run(self, mode):
        with self.db:
            # A previous process may have been killed before its finally block.
            self.db.execute("UPDATE runs SET outcome='interrupted', ended_at=? WHERE outcome='running'", (now(),))
            self.run_id = self.db.execute('INSERT INTO runs(started_at,mode,outcome) VALUES (?,?,?)',
                                          (now(), mode, 'running')).lastrowid
        self.log('run_started', phase='prepare', detail=mode)
        self.set_meta('last_outcome', 'running')

    def finish_run(self, outcome, transferred):
        with self.db:
            self.db.execute('UPDATE runs SET ended_at=?,outcome=?,bytes_transferred=? WHERE id=?',
                            (now(), outcome, transferred, self.run_id))
        self.set_meta('last_outcome', outcome)
        self.log('run_finished', phase='finish', detail=outcome)

    def remember(self, message, scan_id=None, advance=False):
        category = classify_message(message)
        medium = message.photo or message.document
        key = f'{category}:{medium.id}' if category else None
        old = self.db.execute('SELECT * FROM messages WHERE id=?', (message.id,)).fetchone()
        same = bool(old and old['media_key'] == key)
        path = old['path'] if same else None
        size = old['size'] if path else None
        status = old['status'] if same else ('pending' if category else 'none')
        if status in ('unavailable', 'downloading'):
            status = 'pending' if category else 'none'
        raw = message.to_dict()
        expected = expected_size(raw) if category else None
        invalid_file = bool(path and (not (self.root / path).is_file() or (self.root / path).stat().st_size != size
                     or (expected is not None and expected != size)))
        if invalid_file:
            status = 'pending'
        username = self.info['username']
        link = (f'https://t.me/{username}/{message.id}' if username else
                f'https://t.me/c/{str(self.chat_id).removeprefix("-100")}/{message.id}')
        record = dict(id=message.id, date=message.date.isoformat() if message.date else '',
                      text=message.message or '', category=category, media_key=key, path=path,
                      size=size, status=status, error=old['error'] if same else None, link=link,
                      raw=dumps(raw), expected_size=expected, attempts=old['attempts'] if old else 0,
                      availability='available', seen_scan=scan_id or (old['seen_scan'] if old else None),
                      sha256=old['sha256'] if same else None, verified_at=old['verified_at'] if same else None)
        with self.db:
            self.db.execute('''INSERT INTO messages
                (id,date,text,category,media_key,path,size,status,error,link,raw,expected_size,attempts,availability,seen_scan,sha256,verified_at)
                VALUES (:id,:date,:text,:category,:media_key,:path,:size,:status,:error,:link,:raw,:expected_size,:attempts,:availability,:seen_scan,:sha256,:verified_at)
                ON CONFLICT(id) DO UPDATE SET date=excluded.date,text=excluded.text,category=excluded.category,
                media_key=excluded.media_key,path=excluded.path,size=excluded.size,status=excluded.status,
                error=excluded.error,link=excluded.link,raw=excluded.raw,expected_size=excluded.expected_size,
                availability=excluded.availability,seen_scan=excluded.seen_scan,
                sha256=excluded.sha256,verified_at=excluded.verified_at''', record)
            if advance:
                self.state['cursor'] = message.id
                self.state['seen'] += int(not old or old['seen_scan'] != scan_id)
                self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('scan_state', dumps(self.state)))
        if invalid_file and old['status'] == 'done':
            self.log('local_file_requeued', phase='history', message_id=message.id, level='warning',
                     detail='Файл отсутствует или его размер не совпал')
        return category, key, status

    def media_result(self, mid, status, path=None, size=None, error=None, sha256=None):
        with self.db:
            if status == 'done':
                self.db.execute('UPDATE messages SET status=?,path=?,size=?,error=?,sha256=?,verified_at=? WHERE id=?',
                                (status, path, size, error, sha256, now() if sha256 else None, mid))
            else:
                # Preserve the trusted digest and old target across a failed repair.
                self.db.execute('UPDATE messages SET status=?,error=? WHERE id=?', (status, error, mid))

    def unavailable(self, mid, reason):
        with self.db:
            self.db.execute('''UPDATE messages SET availability='unavailable',
                status=CASE WHEN status='done' THEN status ELSE 'unavailable' END,
                error=CASE WHEN status='done' THEN error ELSE ? END WHERE id=?''', (reason, mid))
        self.log('message_unavailable', message_id=mid, level='warning', detail=reason)

    def counts(self):
        return dict(self.db.execute('SELECT status,count(*) FROM messages GROUP BY status'))

    def statistics(self):
        row = self.db.execute('''SELECT count(*) AS archived_messages,
            count(category) AS files_total,
            coalesce(sum(status='done'),0) AS files_done,
            coalesce(sum(category IS NOT NULL AND status NOT IN ('done','unavailable')),0) AS files_pending,
            coalesce(sum(status='error'),0) AS files_errors,
            coalesce(sum(category IS NOT NULL AND status='unavailable'),0) AS files_unavailable,
            coalesce(sum(CASE WHEN category IS NOT NULL THEN coalesce(expected_size,size,0) ELSE 0 END),0) AS total_bytes,
            coalesce(sum(CASE WHEN status='done' THEN size ELSE 0 END),0) AS done_bytes,
            coalesce(sum(category IS NOT NULL AND expected_size IS NULL AND status!='done'),0) AS unknown_sizes
            FROM messages''').fetchone()
        result = dict(row)
        state = self.state or {}
        result.update(history_complete=bool(state.get('history_complete')), history_seen=state.get('seen', 0),
                      history_total_hint=state.get('total_hint'), snapshot_max_id=state.get('upper_id'),
                      scan_cursor=state.get('cursor', 0))
        return result

    def gaps(self):
        """Compressed ID ranges, including a vanished snapshot boundary."""
        if not self.state or not self.state.get('history_complete'):
            return
        scan_id = self.state['id']
        checked = {r['message_id']: r['status'] for r in self.db.execute(
            'SELECT message_id,status FROM gap_checks WHERE scan_id=?', (scan_id,))}
        keys = sorted(checked)
        def split(start, end):
            position = bisect.bisect_left(keys, start)
            while position < len(keys) and keys[position] <= end:
                value = keys[position]
                if value > start:
                    yield start, value - 1, 'unverified'
                last, status = value, checked[value]
                position += 1
                while position < len(keys) and keys[position] == last + 1 and keys[position] <= end and checked[keys[position]] == status:
                    last = keys[position]; position += 1
                yield value, last, status
                start = last + 1
            if start <= end:
                yield start, end, 'unverified'
        previous = None
        for row in self.db.execute('SELECT id FROM messages WHERE seen_scan=? ORDER BY id', (scan_id,)):
            mid = row[0]
            if previous is not None and mid > previous + 1:
                yield from split(previous + 1, mid - 1)
            previous = mid
        upper = self.state['upper_id']
        if previous is not None and previous < upper:
            yield from split(previous + 1, upper)
        elif previous is None and upper:
            # Only the newest ID is known; never invent the size of hidden history.
            yield from split(upper, upper)

    def export(self, complete=False):
        self.info.update(updated_at=now(), scan_complete=complete, counts=self.counts(),
                         snapshot=self.state, progress=self.statistics(),
                         last_outcome=self.get_meta('last_outcome', 'running'))
        gap_counts = {'unavailable': 0, 'unverified': 0}
        with (self.root / 'gaps.csv.tmp').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream); writer.writerow(['from_id', 'to_id', 'count', 'status'])
            for start, end, status in self.gaps():
                writer.writerow([start, end, end-start+1, status])
                gap_counts[status] = gap_counts.get(status, 0) + end-start+1
        (self.root / 'gaps.csv.tmp').replace(self.root / 'gaps.csv')
        self.info['id_gaps'] = gap_counts
        atomic_text(self.root / 'channel.json', dumps(self.info))
        fields = ['id', 'date', 'text', 'category', 'path', 'status', 'error', 'link', 'expected_size', 'attempts', 'availability', 'sha256', 'verified_at']
        with (self.root / 'messages.jsonl.tmp').open('w', encoding='utf-8') as jf, \
             (self.root / 'messages.csv.tmp').open('w', encoding='utf-8-sig', newline='') as cf:
            writer = csv.writer(cf); writer.writerow(fields)
            for row in self.db.execute('SELECT * FROM messages ORDER BY id'):
                record = dict(row); record['telegram'] = json.loads(record.pop('raw'))
                jf.write(dumps(record) + '\n')
                writer.writerow([csv_safe(row[k]) for k in fields])
        for name in ('messages.jsonl', 'messages.csv'):
            (self.root / (name + '.tmp')).replace(self.root / name)
        with (self.root / 'journal.jsonl.tmp').open('w', encoding='utf-8') as stream:
            for row in self.db.execute('SELECT * FROM events ORDER BY id'):
                stream.write(dumps(dict(row)) + '\n')
        (self.root / 'journal.jsonl.tmp').replace(self.root / 'journal.jsonl')
        fields = ['id', 'status', 'attempts', 'error', 'expected_size', 'availability']
        with (self.root / 'errors.csv.tmp').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream); writer.writerow(fields)
            for row in self.db.execute("SELECT * FROM messages WHERE category IS NOT NULL AND status!='done' ORDER BY id"):
                writer.writerow([csv_safe(row[k]) for k in fields])
        (self.root / 'errors.csv.tmp').replace(self.root / 'errors.csv')
        checksums = ''.join(f'{row["sha256"]}  {row["path"]}\n' for row in self.db.execute(
            "SELECT path,sha256 FROM messages WHERE status='done' AND sha256 IS NOT NULL ORDER BY id"))
        atomic_text(self.root / 'SHA256SUMS', checksums)
        self._html_export()
        self._report()

    def _html_export(self):
        total = self.db.execute('SELECT count(*) FROM messages').fetchone()[0]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        cursor = self.db.execute('SELECT * FROM messages ORDER BY id')
        for page in range(pages):
            filename = 'index.html' if page == 0 else f'page_{page + 1}.html'
            prev = 'index.html' if page == 1 else f'page_{page}.html'
            nav = ((f'<a href="{prev}">← Назад</a> · ' if page else '') + f'Страница {page + 1} / {pages}' +
                   (f' · <a href="page_{page + 2}.html">Далее →</a>' if page + 1 < pages else ''))
            parts = [html_head(self.info['title']), f'<h1>{html.escape(self.info["title"])}</h1>',
                     f'<p>{total} сообщений в архиве · <a href="report.html">Состояние и ошибки</a></p><nav>{nav}</nav>']
            for row in cursor.fetchmany(PAGE_SIZE):
                parts.append(f'<article id="m{row["id"]}"><small>{html.escape(row["date"])} · '
                             f'<a href="{html.escape(row["link"], quote=True)}">#{row["id"]}</a></small><pre>{html.escape(row["text"])}</pre>')
                if row['path'] and row['status'] == 'done':
                    url = quote(row['path'])
                    if row['category'] == 'photos': parts.append(f'<img loading="lazy" src="{url}" alt="Фото">')
                    elif row['category'] in ('videos', 'stickers_gifs') and Path(row['path']).suffix.lower() in ('.mp4', '.mov', '.webm'):
                        parts.append(f'<video controls preload="none" src="{url}"></video>')
                    elif row['category'] == 'audio': parts.append(f'<audio controls preload="none" src="{url}"></audio>')
                    parts.append(f'<p><a href="{url}">Открыть файл</a></p>')
                elif row['status'] in ('error', 'pending', 'downloading'):
                    parts.append('<p>Медиа пока не сохранено. Повторите выгрузку.</p>')
                elif row['status'] == 'unavailable':
                    parts.append('<p>Telegram сейчас не возвращает это вложение.</p>')
                parts.append('</article>')
            parts.append(f'<nav>{nav}</nav></html>')
            atomic_text(self.root / filename, ''.join(parts))

    def _report(self):
        stats = self.statistics()
        outcome = self.get_meta('last_outcome', 'running')
        outcome_text = {'complete':'завершено', 'completed_with_issues':'завершено с замечаниями',
                        'cancelled':'остановлено пользователем', 'failed':'прервано из-за ошибки',
                        'interrupted':'процесс был прерван', 'running':'выполняется'}.get(outcome, outcome)
        gaps = self.info.get('id_gaps', {})
        parts = [html_head('Состояние архива'), '<h1>Состояние архива</h1><p><a href="index.html">← Посты</a></p>',
                 f'<p>Последний запуск: {html.escape(outcome_text)}.</p>',
                 f'<p>История: {"собрана" if stats["history_complete"] else "сбор не завершён"}. '
                 f'Файлы: {stats["files_done"]} / {stats["files_total"]}; ошибок: {stats["files_errors"]}; '
                 f'недоступно: {stats["files_unavailable"]}; осталось: {stats["files_pending"]}.</p>',
                 f'<p>Пропуски ID: Telegram не вернул {gaps.get("unavailable", 0)}; '
                 f'не проверено {gaps.get("unverified", 0)}.</p>',
                 '<p><a href="errors.csv">Файлы, требующие внимания (CSV)</a> · '
                 '<a href="gaps.csv">Пропуски ID (CSV)</a> · <a href="journal.jsonl">Полный журнал (JSONL)</a></p>',
                 '<p>Недоступный ID означает, что Telegram сейчас не вернул пост. Это не доказывает, '
                 'что в нём был файл. Непроверенные ID учитываются отдельно.</p>',
                 '<h2>Файлы, требующие внимания</h2><table><tr><th>Пост</th><th>Состояние</th><th>Попыток</th><th>Причина</th></tr>']
        for row in self.db.execute("SELECT id,status,attempts,error FROM messages WHERE category IS NOT NULL AND status!='done' ORDER BY id LIMIT 500"):
            status = {'pending':'Ожидает загрузки', 'error':'Ошибка скачивания',
                      'downloading':'Загружается', 'unavailable':'Недоступно в Telegram'}.get(row['status'], row['status'])
            values = [row['id'], status, row['attempts'], row['error']]
            parts.append('<tr>' + ''.join(f'<td>{html.escape(str(v if v is not None else ""))}</td>' for v in values) + '</tr>')
        parts.append('</table><p>В таблице показано до 500 записей. Полный список — в CSV.</p><h2>Последние события</h2>')
        for row in self.db.execute('SELECT time,message_id,event,detail FROM events ORDER BY id DESC LIMIT 200'):
            label = EVENT_LABELS.get(row['event'], row['event'])
            values = [row['time'], f'пост #{row["message_id"]}' if row['message_id'] is not None else '', label, row['detail']]
            parts.append('<p>' + html.escape(' · '.join(str(v) for v in values if v)) + '</p>')
        parts.append('</html>')
        atomic_text(self.root / 'report.html', ''.join(parts))


def csv_safe(value):
    return "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value


EVENT_LABELS = {
    'schema_migrated':'Формат архива обновлён', 'run_started':'Запуск', 'run_finished':'Завершение запуска',
    'snapshot_started':'Начат сбор истории', 'history_reused':'Используется сохранённая очередь',
    'history_completed':'История собрана', 'history_error':'Ошибка чтения истории',
    'history_retry':'Повтор чтения истории', 'gap_batch_checked':'Проверены номера постов',
    'gap_recovered':'Найден пропущенный пост', 'gap_check_failed':'Не удалось проверить номера',
    'gaps_completed':'Проверка номеров завершена', 'message_unavailable':'Пост недоступен',
    'checksum_created':'Создана контрольная сумма', 'checksum_mismatch':'Обнаружено изменение файла',
    'retry_requested':'Повтор проблемных вложений',
    'local_file_requeued':'Файл возвращён в очередь', 'local_files_checked':'Проверены локальные файлы',
    'download_attempt':'Попытка скачивания', 'file_started':'Начато скачивание файла',
    'file_completed':'Файл сохранён', 'download_error':'Ошибка скачивания',
    'download_retry':'Повтор скачивания', 'file_reference_expired':'Ссылка на файл истекла',
    'flood_wait':'Пауза по требованию Telegram', 'download_cancelled':'Загрузка файла остановлена',
    'run_cancelled':'Остановлено пользователем', 'run_failed':'Запуск прерван из-за ошибки',
    'completed_file_recovered':'Найден ранее завершённый файл', 'media_removed':'Вложение удалено из поста',
    'media_changed':'Вложение изменилось', 'request_error':'Ошибка запроса', 'request_retry':'Повтор запроса',
}


def html_head(title):
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'">
    <title>{html.escape(title)}</title><style>
    body{{max-width:850px;margin:40px auto;padding:0 20px;background:#f6f4ef;color:#252824;font:16px/1.6 -apple-system,sans-serif}}
    article{{border-top:1px solid #d2d4cb;padding:24px 0}}a{{color:#28694e}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}}
    img,video{{max-width:100%;max-height:600px}}small{{color:#647064}}nav{{padding:20px 0}}
    table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;border-bottom:1px solid #ccc;padding:8px;overflow-wrap:anywhere}}
    </style>'''

"""Durable Mini App catalogue and queue; no credentials or signed media URLs."""
import fcntl
import json
import os
import sqlite3
from pathlib import Path

from ..common import now, safe_name
from . import Asset, MiniAppError, identity


class Store:
    def __init__(self, output, source, account_id, bot_id):
        if source != 'edify' or not isinstance(account_id, int) or not isinstance(bot_id, int):
            raise MiniAppError('Некорректный источник архива.')
        self.root = Path(output).expanduser().resolve() / f'miniapp_{source}_{account_id}_{bot_id}'
        if self.root.is_symlink():
            raise MiniAppError('Папка архива не должна быть символической ссылкой.')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = self.path('.archive.lock').open('a')
        self.db = None
        try:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MiniAppError('Этот архив уже открыт в другом процессе.') from None
            self.db = sqlite3.connect(self.path('catalogue.sqlite3'))
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nodes(
                    id TEXT PRIMARY KEY, parent TEXT, kind TEXT, title TEXT, position INTEGER,
                    text TEXT, state TEXT, path TEXT UNIQUE, current INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS assets(
                    id TEXT PRIMARY KEY, node TEXT, label TEXT, filename TEXT, kind TEXT,
                    locator TEXT, fingerprint TEXT, size INTEGER, resumable INTEGER,
                    problem TEXT, path TEXT UNIQUE, status TEXT DEFAULT 'pending',
                    sha256 TEXT, attempts INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    current INTEGER DEFAULT 1);
                CREATE TABLE IF NOT EXISTS runs(
                    id INTEGER PRIMARY KEY, started TEXT, finished TEXT, state TEXT, details TEXT);
            ''')
            owner = {'source':source, 'account_id':str(account_id), 'bot_id':str(bot_id), 'schema':'1'}
            for key, value in owner.items():
                old = self.meta(key)
                if old is not None and old != value:
                    raise MiniAppError('Этот архив принадлежит другому аккаунту или версии приложения.')
                self.db.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', (key, value))
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.lock.close()

    def path(self, relative):
        candidate = self.root / relative
        if candidate.is_absolute() and not candidate.is_relative_to(self.root):
            raise MiniAppError('Некорректный путь файла в архиве.')
        if '..' in Path(relative).parts:
            raise MiniAppError('Некорректный путь файла в архиве.')
        current = candidate
        while current != self.root:
            if current.is_symlink():
                raise MiniAppError('В архиве обнаружена символическая ссылка. Выберите другую папку.')
            current = current.parent
        return candidate

    def write_text(self, relative, value):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path(str(relative) + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def meta(self, key, value=None):
        if value is None:
            row = self.db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, value))

    def begin(self):
        # A killed process can leave an unfinished run; the file lock proves it
        # is no longer running, so make this visible in the history.
        self.db.execute("UPDATE runs SET state='interrupted', finished=? WHERE state='running'", (now(),))
        row = self.db.execute("INSERT INTO runs(started,state,details) VALUES(?,'running','')", (now(),))
        self.db.commit()
        return row.lastrowid

    def finish(self, run, state, details=''):
        self.db.execute('UPDATE runs SET finished=?,state=?,details=? WHERE id=?', (now(),state,details,run))
        self.db.commit()

    def sync(self, catalogue):
        with self.db:
            if catalogue.complete:
                self.db.execute('UPDATE nodes SET current=0')
                self.db.execute('UPDATE assets SET current=0')
            for node in catalogue.nodes:
                old = self.db.execute('SELECT path FROM nodes WHERE id=?', (node.id,)).fetchone()
                if old:
                    path = old['path']
                else:
                    parent = self.db.execute('SELECT path FROM nodes WHERE id=?', (node.parent,)).fetchone()
                    folder = f'{node.position:02d} {safe_name(node.title)}_{identity(node.id)[:12]}'
                    path = str(Path(parent['path']) / folder) if parent else folder
                self.db.execute('''INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET parent=excluded.parent, title=excluded.title,
                    position=excluded.position,
                    text=CASE WHEN excluded.state IN ('unavailable','incomplete') THEN nodes.text ELSE excluded.text END,
                    state=excluded.state, current=1''',
                    (node.id,node.parent,node.kind,node.title,node.position,node.text,node.state,path))
            for asset in catalogue.assets:
                node = self.db.execute('SELECT path FROM nodes WHERE id=?', (asset.node,)).fetchone()
                filename = safe_name(asset.filename)
                suffix = Path(filename).suffix
                filename = safe_name(Path(filename).stem) + '_' + asset.id[:12] + suffix
                path = str(Path(node['path']) / filename)
                self.db.execute('''INSERT INTO assets
                    (id,node,label,filename,kind,locator,fingerprint,size,resumable,problem,path,current)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(id) DO UPDATE SET locator=excluded.locator, filename=excluded.filename,
                    problem=excluded.problem, current=1''',
                    (asset.id,asset.node,asset.label,asset.filename,asset.kind,json.dumps(asset.locator),
                     asset.fingerprint,asset.size,int(asset.resumable),asset.problem,path))
            self.meta('catalogue_complete', '1' if catalogue.complete else '0')
            self.meta('catalogue_warnings', json.dumps(catalogue.warnings, ensure_ascii=False))
            self.meta('catalogue_updated', now())

    def rows(self):
        return self.db.execute('SELECT * FROM assets ORDER BY rowid').fetchall()

    def update(self, asset_id, **values):
        if not values.keys() <= {'status','sha256','attempts','error','size'}:
            raise ValueError('Invalid asset update')
        self.db.execute('UPDATE assets SET ' + ','.join(key+'=?' for key in values) + ' WHERE id=?',
                        (*values.values(), asset_id))
        self.db.commit()

    @staticmethod
    def asset(row):
        return Asset(row['node'],row['label'],row['filename'],row['kind'],json.loads(row['locator']),
                     row['fingerprint'],row['size'],bool(row['resumable']),row['problem'])

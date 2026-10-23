"""Two-phase Telegram archiving: durable history first, resumable media queue second."""
import asyncio
import itertools
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from telethon import errors, utils
from telethon.tl.types import InputPhotoFileLocation

from .common import BLOCK, PAGE_SIZE, atomic_text, classify_message, dumps, now, safe_name
from .storage import Archive

MAX_GAP_CHECK_IDS = 1000
BATCH_SIZE = 100
RETRY_LIMIT = 3


class Progress:
    def __init__(self, archive, emit):
        self.archive, self.emit = archive, emit
        self.phase = 'prepare'
        self.started = time.monotonic()
        self.media_started = None
        self.transferred = 0
        self.active_id = None
        self.active_bytes = 0
        self.active_total = None
        self.waiting = False
        self.stats = archive.statistics()

    def refresh(self):
        self.stats = self.archive.statistics()

    def publish(self):
        elapsed = time.monotonic() - self.started
        media_elapsed = time.monotonic() - self.media_started if self.media_started else 0
        speed = self.transferred / media_elapsed if media_elapsed > 0 and not self.waiting else 0
        remaining = max(0, self.stats['total_bytes'] - self.stats['done_bytes'] - self.active_bytes)
        eta = remaining / speed if speed > 0 and not self.stats['unknown_sizes'] and self.phase == 'media' else None
        self.emit('summary', {**self.stats, 'phase': self.phase, 'elapsed_seconds': elapsed,
                              'speed_bps': speed, 'eta_seconds': eta, 'bytes_transferred': self.transferred,
                              'active_id': self.active_id, 'active_bytes': self.active_bytes,
                              'active_total': self.active_total, 'waiting': self.waiting})

    def stage(self, phase, text):
        self.phase = phase
        if phase == 'media' and self.media_started is None:
            self.media_started = time.monotonic()
        self.refresh(); self.publish(); self.emit('status', text)

    def begin_file(self, mid):
        self.active_id, self.active_bytes, self.active_total = mid, 0, None
        self.publish()

    def file_progress(self, mid, received, total, transferred=0):
        self.transferred += transferred
        self.active_id, self.active_bytes, self.active_total = mid, received, total
        self.emit('progress', (mid, received, total))
        self.publish()

    def finish_file(self, size=None, previous_error=False, unavailable=False, failed=False):
        if size is not None:
            self.stats['files_done'] += 1
            self.stats['done_bytes'] += size
            self.stats['files_pending'] -= 1
        elif unavailable:
            self.stats['files_unavailable'] += 1
            self.stats['files_pending'] -= 1
        if previous_error and (size is not None or unavailable):
            self.stats['files_errors'] -= 1
        if failed and not previous_error:
            self.stats['files_errors'] += 1
        self.active_id, self.active_bytes, self.active_total = None, 0, None
        self.publish()


async def pause(archive, progress, seconds, phase, mid=None, error=None):
    archive.log('flood_wait', phase=phase, message_id=mid, level='warning',
                detail=str(error or 'Telegram FloodWait'), delay=seconds)
    progress.waiting = True; progress.publish()
    progress.emit('log', f'Telegram запросил паузу {seconds} с. Работа продолжится автоматически.')
    try:
        await asyncio.sleep(seconds + 1)
    finally:
        progress.waiting = False; progress.publish()


async def rpc_call(operation, archive, progress, phase, mid=None):
    failures = 0
    while True:
        try:
            return await operation()
        except errors.FloodWaitError as exc:
            await pause(archive, progress, exc.seconds, phase, mid, exc)
        except (ConnectionError, asyncio.TimeoutError, errors.ServerError) as exc:
            failures += 1
            archive.log('request_error', phase=phase, message_id=mid, level='error',
                        detail=f'{type(exc).__name__}: {exc}', attempt=failures)
            if failures >= RETRY_LIMIT:
                raise
            archive.log('request_retry', phase=phase, message_id=mid, attempt=failures + 1, delay=2 * failures)
            await asyncio.sleep(2 * failures)


async def collect_history(client, entity, archive, progress, mode):
    state = archive.state
    if mode == 'full' or state is None:
        newest = await rpc_call(lambda: client.get_messages(entity, limit=1), archive, progress, 'history')
        upper = newest[0].id if newest else 0
        archive.state = dict(id=uuid.uuid4().hex, upper_id=upper, cursor=0, seen=0,
                             total_hint=getattr(newest, 'total', None), history_complete=False,
                             gaps_complete=False, started_at=now())
        archive.save_state()
        archive.log('snapshot_started', phase='history', detail=f'Upper message ID: {upper}')
    if archive.state['history_complete']:
        archive.log('history_reused', phase='history', detail='Using persisted media queue without re-reading history')
        return
    progress.stage('history', 'Собираю историю и список вложений…')
    failures = 0
    while True:
        try:
            if archive.state['upper_id']:
                async for message in client.iter_messages(entity, reverse=True,
                    min_id=archive.state['cursor'], max_id=archive.state['upper_id'] + 1, wait_time=1):
                    archive.remember(message, archive.state['id'], advance=True)
                    failures = 0
                    progress.stats['history_seen'] = archive.state['seen']
                    progress.stats['scan_cursor'] = archive.state['cursor']
                    if archive.state['seen'] % 25 == 0:
                        progress.refresh(); progress.publish()
                    # Cancellation remains responsive even if history pages are cached locally.
                    if archive.state['seen'] % 100 == 0:
                        await asyncio.sleep(0)
            missing = archive.db.execute('''SELECT id FROM messages
                WHERE (seen_scan IS NULL OR seen_scan!=?) AND availability='available' ''',
                (archive.state['id'],)).fetchall()
            for row in missing:
                archive.unavailable(row['id'], 'Пост не вернулся при полной сверке истории')
            archive.state['history_complete'] = True
            archive.state['finished_at'] = now()
            archive.save_state()
            archive.log('history_completed', phase='history', detail=f'Messages collected: {archive.state["seen"]}')
            progress.refresh(); progress.publish()
            return
        except errors.FloodWaitError as exc:
            await pause(archive, progress, exc.seconds, 'history', error=exc)
        except (ConnectionError, asyncio.TimeoutError, errors.ServerError) as exc:
            failures += 1
            archive.log('history_error', phase='history', level='error',
                        detail=f'{type(exc).__name__}: {exc}', attempt=failures)
            if failures >= RETRY_LIMIT:
                raise
            archive.log('history_retry', phase='history', attempt=failures + 1, delay=2 * failures)
            await asyncio.sleep(2 * failures)


async def check_gaps(client, entity, archive, progress):
    if archive.state.get('gaps_complete'):
        return
    progress.stage('gaps', 'Проверяю пропущенные номера постов…')
    # A very large numerical ID gap must not cause millions of Telegram requests.
    checked = {r[0] for r in archive.db.execute('SELECT message_id FROM gap_checks WHERE scan_id=?',
                                               (archive.state['id'],))}
    pending = list(itertools.islice((mid for start, end, state in archive.gaps()
        for mid in range(start, end + 1) if mid not in checked), max(0, MAX_GAP_CHECK_IDS - len(checked))))
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        try:
            messages = await rpc_call(lambda: client.get_messages(entity, ids=batch), archive, progress, 'gaps')
        except errors.RPCError as exc:
            archive.log('gap_check_failed', phase='gaps', level='warning', detail=f'{type(exc).__name__}: {exc}')
            break
        found = {message.id: message for message in messages if message is not None and hasattr(message, 'message')}
        for mid in batch:
            if mid in found:
                archive.remember(found[mid], archive.state['id'])
                archive.log('gap_recovered', phase='gaps', message_id=mid)
            with archive.db:
                archive.db.execute('INSERT OR REPLACE INTO gap_checks VALUES (?,?,?,?)',
                    (mid, archive.state['id'], 'available' if mid in found else 'unavailable', now()))
        archive.log('gap_batch_checked', phase='gaps', detail=f'Checked {len(batch)} IDs; found {len(found)} messages')
        if start + BATCH_SIZE < len(pending):
            await asyncio.sleep(1)
    archive.state['seen'] = archive.db.execute('SELECT count(*) FROM messages WHERE seen_scan=?',
                                              (archive.state['id'],)).fetchone()[0]
    archive.state['gaps_complete'] = True
    archive.save_state()
    gap_counts = {'unavailable': 0, 'unverified': 0}
    for start, end, state in archive.gaps(): gap_counts[state] = gap_counts.get(state, 0) + end-start+1
    archive.log('gaps_completed', phase='gaps', detail=dumps(gap_counts))
    progress.emit('log', f'Пропуски ID: недоступно {gap_counts["unavailable"]}, '
                         f'не проверено {gap_counts["unverified"]}. Это не число потерянных файлов.')


async def verify_local_files(archive):
    repaired = 0
    for index, row in enumerate(archive.db.execute("SELECT id,path,size,expected_size FROM messages WHERE status='done'")):
        path = archive.root / row['path'] if row['path'] else None
        if (path is None or not path.is_file() or path.stat().st_size != row['size'] or
                (row['expected_size'] is not None and row['expected_size'] != row['size'])):
            with archive.db:
                archive.db.execute("UPDATE messages SET status=CASE WHEN availability='available' THEN 'pending' ELSE 'unavailable' END WHERE id=?", (row['id'],))
            archive.log('local_file_requeued', message_id=row['id'], phase='prepare', level='warning',
                        detail='Файл отсутствует или его размер не совпал')
            repaired += 1
        if index % 100 == 0:
            await asyncio.sleep(0)
    archive.log('local_files_checked', phase='prepare', detail=f'Requeued: {repaired}')


def file_target(message, archive, category, key):
    info = message.file
    ext = info.ext or '.bin'
    original = safe_name(info.name or f'{category}{ext}')
    if not Path(original).suffix:
        original += ext
    return archive.root / 'media' / category / f'{message.id}_{key.split(":")[-1]}_{original}'


async def download_file(client, message, archive, category, key, emit, progress=None):
    target = file_target(message, archive, category, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.part')
    expected = message.file.size
    location = message.media
    if message.photo:
        photo = message.photo
        rendition = max(photo.sizes, key=lambda s: utils._photo_size_byte_count(s) or 0)
        expected = utils._photo_size_byte_count(rendition)
        location = InputPhotoFileLocation(id=photo.id, access_hash=photo.access_hash,
                                          file_reference=photo.file_reference, thumb_size=rendition.type)
    # A crash can happen after the final rename and before the SQLite update.
    if expected is not None and target.is_file() and target.stat().st_size == expected:
        archive.media_result(message.id, 'done', target.relative_to(archive.root).as_posix(), expected)
        archive.log('completed_file_recovered', message_id=message.id)
        return expected
    offset = partial.stat().st_size // BLOCK * BLOCK if partial.exists() else 0
    if expected is not None and offset > expected:
        offset = 0
    needed = max(0, (expected or 0) - offset)
    if needed and shutil.disk_usage(target.parent).free < needed + 10 * 1024 * 1024:
        raise OSError(28, 'Недостаточно места на диске')
    archive.log('file_started', message_id=message.id, detail=f'offset={offset}; expected_bytes={expected}')
    if progress:
        progress.file_progress(message.id, offset, expected)
    last_emit = 0
    loop = asyncio.get_running_loop()
    with partial.open('r+b' if partial.exists() else 'w+b') as stream:
        stream.truncate(offset); stream.seek(offset)
        async for chunk in client.iter_download(location, offset=offset, request_size=BLOCK,
                file_size=expected, dc_id=getattr(message.photo or message.document, 'dc_id', None)):
            stream.write(chunk)
            offset += len(chunk)
            if progress:
                progress.transferred += len(chunk)
                progress.active_bytes = offset
                progress.active_total = expected
            current = loop.time()
            if current - last_emit > .3:
                emit('progress', (message.id, offset, expected))
                if progress: progress.publish()
                last_emit = current
        stream.flush(); os.fsync(stream.fileno())
    if expected is not None and offset != expected:
        raise OSError(f'Размер файла не совпал: {offset} / {expected}')
    if offset == 0 and expected != 0:
        raise OSError('Telegram вернул пустой файл')
    partial.replace(target)
    archive.media_result(message.id, 'done', target.relative_to(archive.root).as_posix(), offset)
    emit('progress', (message.id, offset, expected))
    archive.log('file_completed', message_id=message.id, detail=f'bytes={offset}')
    return offset


async def fetch_messages(client, entity, ids, archive, progress):
    messages = await rpc_call(lambda: client.get_messages(entity, ids=ids), archive, progress, 'media')
    return {m.id: m for m in messages if m is not None and hasattr(m, 'message')}


async def download_queue(client, entity, archive, progress):
    progress.stage('media', 'Скачиваю вложения из сохранённой очереди…')
    cursor = 0
    while True:
        rows = archive.db.execute('''SELECT id,status FROM messages WHERE id>? AND category IS NOT NULL
            AND availability='available' AND status!='done' ORDER BY id LIMIT ?''', (cursor, BATCH_SIZE)).fetchall()
        if not rows:
            return
        found = await fetch_messages(client, entity, [r['id'] for r in rows], archive, progress)
        for row in rows:
            mid = row['id']; cursor = mid
            previous_error = row['status'] == 'error'
            progress.begin_file(mid)
            message = found.get(mid)
            if message is None:
                archive.unavailable(mid, 'Telegram не вернул пост при получении вложения')
                progress.finish_file(previous_error=previous_error, unavailable=True)
                continue
            before = archive.db.execute('SELECT media_key,expected_size FROM messages WHERE id=?', (mid,)).fetchone()
            category, key, status = archive.remember(message)
            if not category:
                archive.log('media_removed', message_id=mid, detail='Во время загрузки у поста больше нет вложения')
                progress.refresh(); progress.active_id = None; progress.publish()
                continue
            after = archive.db.execute('SELECT expected_size FROM messages WHERE id=?', (mid,)).fetchone()[0]
            if before['media_key'] != key or before['expected_size'] != after:
                archive.log('media_changed', message_id=mid, detail='Вложение изменилось; очередь обновлена')
                progress.refresh()
                previous_error = status == 'error'
            failures = 0
            try:
                while True:
                    with archive.db:
                        archive.db.execute("UPDATE messages SET status='downloading',attempts=attempts+1 WHERE id=?", (mid,))
                    attempt = archive.db.execute('SELECT attempts FROM messages WHERE id=?', (mid,)).fetchone()[0]
                    archive.log('download_attempt', message_id=mid, attempt=attempt)
                    try:
                        size = await download_file(client, message, archive, category, key, progress.emit, progress)
                        progress.finish_file(size=size, previous_error=previous_error)
                        break
                    except errors.FloodWaitError as exc:
                        await pause(archive, progress, exc.seconds, 'media', mid, exc)
                    except errors.FileReferenceExpiredError as exc:
                        failures += 1
                        archive.log('file_reference_expired', message_id=mid, level='warning',
                                    detail=f'{type(exc).__name__}: {exc}', attempt=attempt)
                        if failures >= RETRY_LIMIT:
                            archive.media_result(mid, 'error', error=f'{type(exc).__name__}: {exc}')
                            progress.finish_file(previous_error=previous_error, failed=True)
                            break
                        refreshed = await fetch_messages(client, entity, [mid], archive, progress)
                        message = refreshed.get(mid)
                        if message is None:
                            archive.unavailable(mid, 'Пост недоступен при обновлении ссылки на файл')
                            progress.finish_file(previous_error=previous_error, unavailable=True)
                            break
                        category, key, status = archive.remember(message)
                        progress.refresh()
                        previous_error = status == 'error'
                        if not category:
                            progress.active_id = None; progress.publish(); break
                        archive.log('download_retry', message_id=mid, detail='Обновлена ссылка Telegram', attempt=attempt + 1)
                    except (OSError, errors.RPCError, asyncio.TimeoutError) as exc:
                        failures += 1
                        detail = f'{type(exc).__name__}: {exc}'
                        archive.media_result(mid, 'error', error=detail)
                        archive.log('download_error', message_id=mid, level='error', detail=detail, attempt=attempt)
                        if isinstance(exc, OSError) and exc.errno in (5, 6, 13, 28, 30):
                            raise
                        if failures >= RETRY_LIMIT:
                            progress.emit('log', f'Файл #{mid} не скачан после {failures} попыток. Причина сохранена в журнале.')
                            progress.finish_file(previous_error=previous_error, failed=True)
                            break
                        delay = 2 * failures
                        archive.log('download_retry', message_id=mid, attempt=attempt + 1, delay=delay)
                        await asyncio.sleep(delay)
            except asyncio.CancelledError:
                with archive.db:
                    archive.db.execute("UPDATE messages SET status='pending' WHERE id=? AND status='downloading'", (mid,))
                archive.log('download_cancelled', message_id=mid, detail=f'Partial bytes observed: {progress.active_bytes}')
                raise


async def export_channel(client, entity, output, emit, mode='full'):
    if mode not in ('full', 'resume'):
        raise ValueError('Неизвестный режим выгрузки')
    me = await client.get_me()
    archive = Archive(output, entity, me.id)
    progress = Progress(archive, emit)
    emit('archive', str(archive.root))
    outcome = 'failed'
    try:
        archive.start_run(mode)
        await collect_history(client, entity, archive, progress, mode)
        # All post texts and the durable queue are exported before any media transfer.
        progress.stage('export', 'Сохраняю собранную историю…')
        archive.export(False)
        await check_gaps(client, entity, archive, progress)
        await verify_local_files(archive)
        archive.export(False)
        await download_queue(client, entity, archive, progress)
        progress.refresh()
        stats = progress.stats
        outcome = 'complete' if not stats['files_pending'] and not stats['files_unavailable'] else 'completed_with_issues'
        emit('status', 'Готово' if outcome == 'complete' else 'Обход завершён; часть вложений требует внимания. Откройте отчёт.')
    except asyncio.CancelledError:
        outcome = 'cancelled'
        archive.log('run_cancelled', phase=progress.phase, detail='Остановка пользователем')
        raise
    except Exception as exc:
        archive.log('run_failed', phase=progress.phase, level='error', detail=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            archive.finish_run(outcome, progress.transferred)
            progress.stage('cancelled' if outcome == 'cancelled' else 'finished',
                           'Сохраняю результаты остановки…' if outcome == 'cancelled' else 'Сохраняю результаты…')
            archive.export(outcome == 'complete')
            emit('log', f'В архиве: {progress.stats["archived_messages"]} сообщений; '
                        f'файлов: {progress.stats["files_done"]}/{progress.stats["files_total"]}; '
                        f'ошибок: {progress.stats["files_errors"]}; недоступно: {progress.stats["files_unavailable"]}.')
            if outcome in ('complete', 'completed_with_issues'):
                emit('status', 'Готово' if outcome == 'complete' else 'Часть файлов требует внимания — откройте «Ошибки и журнал».')
        except Exception as exc:
            if not active_exception:
                raise
            emit('log', f'Экспорт не завершён: {type(exc).__name__}: {exc}. База сохраняет очередь; повторите запуск.')
        finally:
            archive.close()
    return archive.root

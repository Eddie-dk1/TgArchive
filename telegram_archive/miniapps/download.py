"""HTTP downloads with strict Range validation, bounded memory and safe restart."""
import asyncio
import os
import re
import shutil
from urllib.parse import urljoin

import httpx

from ..integrity import file_sha256
from . import AccessDenied, MiniAppError
from .edify import APP, validate_url


async def download(provider, store, asset, emit=lambda *_: None):
    row = store.db.execute('SELECT * FROM assets WHERE id=?', (asset.id,)).fetchone()
    target = store.path(row['path'])
    part = store.path(row['path'] + '.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and row['sha256'] and target.stat().st_size == row['size']:
        emit('status', 'Проверяю SHA-256: ' + asset.filename)
        if await file_sha256(target) == row['sha256']:
            store.update(asset.id, status='done', error='')
            return False
    if target.exists():
        # Keep the previous file until its replacement has been verified.
        store.update(asset.id, status='pending', error='Локальный файл отсутствует или не прошёл проверку SHA-256.')
    elif row['status'] == 'done':
        store.update(asset.id, status='pending', error='Сохранённый файл отсутствует.')
    if asset.problem:
        store.update(asset.id, status='unsupported', error=asset.problem)
        return False
    attempt = 0
    while attempt < 3:
        row = store.db.execute('SELECT * FROM assets WHERE id=?', (asset.id,)).fetchone()
        store.update(asset.id, attempts=row['attempts'] + 1, status='pending')
        try:
            expected = asset.size
            offset = part.stat().st_size if part.exists() else 0
            if not asset.resumable or expected is None or offset > expected:
                offset = 0
            if part.exists() and offset == 0:
                part.unlink()
            # A completed .part may have been left just before hashing/rename.
            if expected is None or offset != expected or not part.exists():
                remaining = max(0, expected - offset) if expected is not None else 1024 * 1024
                if shutil.disk_usage(target.parent).free < remaining + 16 * 1024 * 1024:
                    raise MiniAppError('Недостаточно свободного места для файла.')
                url = await provider.resolve(asset)
                headers = {'Origin': APP.rstrip('/'), 'Referer': APP, 'Accept-Encoding':'identity', 'Cookie':''}
                if offset:
                    headers['Range'] = f'bytes={offset}-'
                # No Authorization header or cookies on media requests. Redirect
                # targets are validated individually, without forwarding secrets.
                for redirect in range(6):
                    async with provider.http.stream('GET', validate_url(url), headers=headers) as response:
                        if response.status_code in (301,302,303,307,308):
                            url = urljoin(url, response.headers.get('location', ''))
                            continue
                        if response.status_code in (401,403,404,410):
                            raise AccessDenied('Ссылка на материал недоступна или истекла. Повторите загрузку.')
                        if response.status_code not in (200,206):
                            raise MiniAppError(f'Не удалось скачать материал (HTTP {response.status_code}).')
                        if response.headers.get('content-encoding', 'identity') != 'identity':
                            raise MiniAppError('Сервер прислал сжатый поток вместо исходного файла.')
                        length = response.headers.get('content-length')
                        length = int(length) if length is not None else None
                        piece_end = None
                        if response.status_code == 206:
                            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('content-range', ''))
                            if not match:
                                raise MiniAppError('Некорректный Content-Range. Файл не объединён.')
                            start, end, total = map(int, match.groups())
                            if start != offset or end < start or end >= total or (expected is not None and total != expected):
                                raise MiniAppError('Размер или позиция материала изменились. Файл не объединён.')
                            if length is not None and length != end - start + 1:
                                raise MiniAppError('Content-Length не совпадает с Content-Range.')
                            expected = total
                            piece_end = end + 1
                            if offset:
                                emit('log', f'Докачка {asset.filename}: сервер подтвердил HTTP 206 с байта {offset}.')
                        else:
                            # The server ignored Range: truncate, never append.
                            offset = 0
                            if expected is not None and length is not None and length != expected:
                                raise MiniAppError('Размер материала изменился. Обновите каталог.')
                            expected = length if expected is None else expected
                        content_type = response.headers.get('content-type', '').split(';')[0].lower()
                        if content_type in ('text/html','application/json') and asset.kind in ('video','photo'):
                            raise MiniAppError('Вместо медиа сервер вернул страницу или сообщение об ошибке.')
                        received = offset
                        with part.open('ab' if offset else 'wb') as stream:
                            try:
                                async for chunk in response.aiter_raw(256 * 1024):
                                    stream.write(chunk)
                                    received += len(chunk)
                                    if (piece_end is not None and received > piece_end) or (expected is not None and received > expected):
                                        raise MiniAppError('Поток превысил ожидаемый размер файла.')
                                    emit('progress', (asset.id[:8],received,expected))
                                    await asyncio.sleep(0)
                            finally:
                                stream.flush()
                                os.fsync(stream.fileno())
                        if piece_end is not None and received != piece_end:
                            raise MiniAppError('Соединение оборвалось до окончания порции файла.')
                        if piece_end is None and expected is not None and received != expected:
                            raise MiniAppError('Соединение оборвалось до окончания файла.')
                        if expected is None:
                            expected = received
                        break
                else:
                    raise MiniAppError('Слишком много перенаправлений при скачивании.')
                if received < expected:
                    if not asset.resumable:
                        raise MiniAppError('Сервер отдаёт файл порциями без стабильной версии. Автодокачка недоступна.')
                    # Edify deliberately limits an open-ended range to a small
                    # piece. A complete piece is progress, not a failed attempt.
                    attempt = 0
                    continue
            emit('status', 'Проверяю SHA-256: ' + asset.filename)
            digest = await file_sha256(part)
            if part.stat().st_size != expected:
                raise MiniAppError('Неполный файл не может быть отмечен как сохранённый.')
            part.replace(target)
            store.update(asset.id, status='done', sha256=digest, size=expected, error='')
            return True
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OSError, MiniAppError, ValueError) as exc:
            # HTTPX's exception string can contain a signed URL; never persist it.
            message = str(exc) if isinstance(exc, MiniAppError) else 'Ошибка сети или диска: ' + type(exc).__name__
            store.update(asset.id, status='unavailable' if isinstance(exc, AccessDenied) else 'error', error=message)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            attempt += 1
    return False

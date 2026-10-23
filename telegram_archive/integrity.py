"""Cooperative, bounded-memory hashing of completed files."""
import asyncio
import hashlib
import os


async def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            await asyncio.sleep(0)
        after = os.fstat(stream.fileno())
    current = path.stat()
    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if signature(before) != signature(after) or signature(after) != signature(current):
        raise OSError('Файл изменился во время проверки SHA-256. Повторите проверку.')
    return digest.hexdigest()

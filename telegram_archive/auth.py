"""Persistent-session login, including local QR rendering without external services."""
import asyncio
import hashlib
import json
import re
from pathlib import Path

from telethon import errors

from .core import atomic_text, dumps


def forget_local_account(private):
    """Local sign-out only. Never contact Telegram or revoke an authorization key."""
    private = Path(private)
    settings = private / 'settings.json'
    try:
        config = json.loads(settings.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        config = {}
    # Persist this first so a failed file removal cannot silently reconnect on launch.
    config.update(phone='', login_method='QR-код', auto_connect=False)
    atomic_text(settings, dumps(config))
    settings.chmod(0o600)
    for name in ('desktop_connection.json', 'desktop_connection.json.tmp'):
        (private / name).unlink(missing_ok=True)
    managed = re.compile(r'(?:account|[a-f0-9]{20}|desktop_\d+)\.session(?:-journal|-wal|-shm)?')
    for path in private.iterdir():
        if managed.fullmatch(path.name) and (path.is_file() or path.is_symlink()):
            # unlink removes a symlink itself, never the file it points to.
            path.unlink()


def session_key(config):
    # Reuse sessions created by the original phone-only version.
    phone = re.sub(r'[\s()-]', '', config.get('phone', ''))
    return hashlib.sha256(phone.encode()).hexdigest()[:20] if phone else 'account'


async def authorize_qr(client, emit, prompt):
    await client.connect()
    if await client.is_user_authorized():
        return await client.get_me()
    try:
        while True:
            qr = await client.qr_login()
            waiter = asyncio.create_task(qr.wait())
            try:
                # Register the Telegram update listener before displaying the QR.
                await asyncio.sleep(0)
                emit('qr', qr.url)
                return await waiter
            except asyncio.TimeoutError:
                emit('status', 'QR-код обновлён. Отсканируйте его в Telegram на телефоне.')
            except errors.SessionPasswordNeededError:
                emit('qr_close', None)
                for attempt in range(3):
                    password = await prompt('Пароль 2FA', 'Введите облачный пароль Telegram:', True)
                    try:
                        return await client.sign_in(password=password)
                    except errors.PasswordHashInvalidError:
                        if attempt == 2:
                            raise
                        emit('log', 'Пароль 2FA не подошёл. Попробуйте ещё раз.')
            finally:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
    finally:
        emit('qr_close', None)

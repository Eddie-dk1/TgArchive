"""Read a Telegram Desktop session locally; never write to the source tdata."""
import json
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, SQLiteSession

from .core import atomic_text, dumps


def find_tdata():
    home = Path.home()
    candidates = [home / 'Library/Application Support/Telegram Desktop/tdata',
                  home / 'Library/Containers/org.telegram.desktop/Data/Library/Application Support/Telegram Desktop/tdata',
                  home / 'Library/Containers/ru.keepcoder.Telegram/Data/Library/Application Support/Telegram Desktop/tdata']
    return [path for path in candidates if path.is_dir() and any(
        (path / name).is_file() for name in ('key_datas', 'key_data0', 'key_data1'))]


def load_tdata(path, passcode=None):
    from opentele2.td import TDesktop
    path = Path(path).expanduser().resolve()
    if not path.is_dir() or not any((path / n).is_file() for n in ('key_datas', 'key_data0', 'key_data1')):
        raise ValueError('В этой папке нет сессии Telegram Desktop. Выберите папку tdata.')
    try:
        desktop = TDesktop(str(path), passcode=passcode)
    except UnicodeEncodeError:
        raise ValueError('Эта версия импорта не поддерживает символы вне ASCII в локальном код-пароле. '
                         'Используйте QR-вход.') from None
    if not desktop.isLoaded() or not desktop.accounts:
        raise ValueError('В Telegram Desktop не найдено вошедших аккаунтов.')
    return desktop


def connection_info(private):
    path = Path(private) / 'desktop_connection.json'
    if not path.exists():
        return None
    info = json.loads(path.read_text(encoding='utf-8'))
    if not re.fullmatch(r'desktop_\d+', info.get('session_name', '')):
        raise ValueError('Некорректные настройки Desktop. Подключите Telegram Desktop повторно.')
    return info


def saved_client(private, info, **kwargs):
    client = TelegramClient(str(Path(private) / info['session_name']), info['api_id'], info['api_hash'],
                            **info['client_options'], **kwargs)
    client._init_request.lang_pack = info.get('lang_pack', 'tdesktop')
    return client


async def convert_account(account, private):
    """Offline conversion only. No login, network or updates to Desktop files."""
    from opentele2.api import UseCurrentSession
    from opentele2.tl import TelegramClient as Converter
    converted = await Converter.FromTDesktop(account, session=MemorySession(),
                                            flag=UseCurrentSession, auto_post_login=False)
    try:
        name = f'desktop_{int(account.UserId)}'
        private = Path(private)
        private.mkdir(mode=0o700, exist_ok=True)
        private.chmod(0o700)
        target = SQLiteSession(str(private / name))
        try:
            source = converted.session
            target.set_dc(source.dc_id, source.server_address, source.port)
            target.auth_key = AuthKey(source.auth_key.key)
            target.save()
        finally:
            target.close()
        (private / (name + '.session')).chmod(0o600)
        api = account.api
        options = {key: getattr(api, key) for key in
                   ('device_model', 'system_version', 'app_version', 'lang_code', 'system_lang_code')
                   if getattr(api, key, None) is not None}
        return {'session_name': name, 'account_id': int(account.UserId),
                'api_id': converted.api_id, 'api_hash': converted.api_hash,
                'client_options': options, 'lang_pack': getattr(api, 'lang_pack', 'tdesktop')}
    finally:
        await converted.disconnect()


async def import_desktop(path, private, prompt, emit, **kwargs):
    from opentele2.exception import TDataBadDecryptKey
    passcode = None
    for attempt in range(3):
        try:
            desktop = load_tdata(path, passcode)
            break
        except TDataBadDecryptKey:
            if attempt == 2:
                raise ValueError('Локальный код-пароль не подошёл. Повторите подключение.') from None
            passcode = await prompt('Код-пароль Telegram Desktop',
                                   'Введите локальный код-пароль Desktop (не облачный пароль 2FA):', True)
    accounts = desktop.accounts
    account = desktop.mainAccount
    if len(accounts) > 1:
        options = '\n'.join(f'{i + 1}. Аккаунт ID {a.UserId}' for i, a in enumerate(accounts))
        while True:
            choice = await prompt('Выберите аккаунт Desktop', options + '\n\nВведите номер аккаунта из списка:')
            if choice.isdigit() and 1 <= int(choice) <= len(accounts):
                account = accounts[int(choice) - 1]
                break
    info = await convert_account(account, private)
    client = saved_client(private, info, **kwargs)
    try:
        emit('status', 'Проверяю подключение через сессию Telegram Desktop…')
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError('Сессия Desktop уже недействительна. Войдите в Telegram Desktop и повторите подключение.')
        me = await client.get_me()
        if me.id != info['account_id']:
            raise ValueError('Аккаунт сессии не совпал. Повторите подключение Desktop.')
        # Publish the connection only after verifying it with Telegram.
        atomic_text(Path(private) / 'desktop_connection.json', dumps(info))
        (Path(private) / 'desktop_connection.json').chmod(0o600)
        emit('log', 'Подключён аккаунт Telegram Desktop. Сессия сохранена в папке приложения.')
        return client
    except BaseException:
        await client.disconnect()
        raise

import hashlib
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from opentele2.api import API, UseCurrentSession
from opentele2.exception import TDataBadDecryptKey
from opentele2.tl import TelegramClient as Converter
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, SQLiteSession

from telegram_archive.desktop import convert_account, load_tdata, connection_info, import_desktop


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.key = bytes(range(256))  # Artificial test key, never sent to Telegram.
        memory = MemorySession()
        memory.set_dc(2, '149.154.167.51', 443)
        memory.auth_key = AuthKey(self.key)
        source = Converter(memory, api=API.TelegramDesktop, auto_post_login=False)
        source.UserId = 12345
        with patch.object(source, 'connect', new=AsyncMock(side_effect=AssertionError('network forbidden'))):
            self.desktop = await source.ToTDesktop(flag=UseCurrentSession)
        self.tdata = self.root / 'tdata'
        self.desktop.SaveTData(str(self.tdata))
        await source.disconnect()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        return {p.relative_to(self.tdata).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.tdata.rglob('*') if p.is_file()}

    async def test_real_format_roundtrip_without_network_or_source_changes(self):
        before = self.snapshot()
        loaded = load_tdata(self.tdata)
        self.assertEqual(loaded.mainAccount.UserId, 12345)
        with patch.object(Converter, 'connect', new=AsyncMock(side_effect=AssertionError('network forbidden'))):
            info = await convert_account(loaded.mainAccount, self.root / 'private')
        target_path = self.root / 'private' / (info['session_name'] + '.session')
        session = SQLiteSession(str(target_path))
        try:
            self.assertEqual(session.auth_key.key, self.key)
            self.assertEqual(session.dc_id, 2)
        finally:
            session.close()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(target_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(info['account_id'], 12345)
        self.assertEqual(info['api_id'], API.TelegramDesktop.api_id)

    async def test_local_passcode_required(self):
        self.desktop.SaveTData(str(self.tdata), passcode='1234')
        before = self.snapshot()
        with self.assertRaises(TDataBadDecryptKey):
            load_tdata(self.tdata)
        loaded = load_tdata(self.tdata, '1234')
        self.assertEqual(loaded.mainAccount.UserId, 12345)
        self.assertEqual(before, self.snapshot())

    async def test_missing_folder_does_not_get_created(self):
        path = self.root / 'missing'
        with self.assertRaises(ValueError):
            load_tdata(path)
        self.assertFalse(path.exists())

    async def test_reject_invalid_connection_session_path(self):
        (self.root / 'desktop_connection.json').write_text('{"session_name":"../../secret"}')
        with self.assertRaises(ValueError):
            connection_info(self.root)

    async def test_invalid_session_not_published(self):
        fake = AsyncMock()
        fake.is_user_authorized.return_value = False
        with patch('telegram_archive.desktop.saved_client', return_value=fake):
            with self.assertRaisesRegex(ValueError, 'недействительна'):
                await import_desktop(self.tdata, self.root / 'private', AsyncMock(), lambda *x: None)
        fake.disconnect.assert_awaited_once()
        self.assertFalse((self.root / 'private' / 'desktop_connection.json').exists())

    async def test_successful_import_publishes_verified_account(self):
        fake = AsyncMock()
        fake.is_user_authorized.return_value = True
        fake.get_me.return_value = SimpleNamespace(id=12345)
        private = self.root / 'private'
        before = self.snapshot()
        with patch('telegram_archive.desktop.saved_client', return_value=fake):
            result = await import_desktop(self.tdata, private, AsyncMock(), lambda *x: None)
        self.assertIs(result, fake)
        self.assertEqual(connection_info(private)['account_id'], 12345)
        self.assertEqual((private / 'desktop_connection.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(before, self.snapshot())

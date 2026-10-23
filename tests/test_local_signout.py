import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_archive.auth import forget_local_account


class LocalSignoutTests(unittest.TestCase):
    def test_signout_removes_archive_access_without_touching_desktop_or_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / 'archive-app' / '.private'
            private.mkdir(parents=True)
            desktop = root / 'Telegram Desktop' / 'tdata'
            desktop.mkdir(parents=True)
            desktop_key = desktop / 'key_datas'
            desktop_key.write_bytes(b'artificial Desktop authorization')
            export = root / 'downloads' / 'messages.jsonl'
            export.parent.mkdir()
            export.write_text('{"text":"saved post"}\n')
            config = dict(phone='+79991234567', login_method='Telegram Desktop',
                          api_id='123', api_hash='application credential', proxy_host='127.0.0.1')
            (private / 'settings.json').write_text(json.dumps(config))
            sessions = ['desktop_123.session', 'desktop_123.session-wal', 'desktop_123.session-shm',
                        'account.session', 'a' * 20 + '.session-journal', 'a' * 20 + '.session']
            for name in sessions:
                (private / name).write_bytes(b'fake local auth key')
            (private / 'desktop_connection.json').write_text('{"session_name":"desktop_123"}')
            (private / 'app.lock').write_bytes(b'')
            with patch('telethon.TelegramClient.log_out', side_effect=AssertionError('Must not revoke server session')):
                forget_local_account(private)
            self.assertFalse(any((private / n).exists() for n in sessions))
            self.assertFalse((private / 'desktop_connection.json').exists())
            self.assertEqual(desktop_key.read_bytes(), b'artificial Desktop authorization')
            self.assertEqual(export.read_text(), '{"text":"saved post"}\n')
            self.assertTrue((private / 'app.lock').exists())
            saved = json.loads((private / 'settings.json').read_text())
            self.assertEqual(saved['phone'], '')
            self.assertFalse(saved['auto_connect'])
            self.assertEqual(saved['api_id'], '123')
            self.assertEqual(saved['proxy_host'], '127.0.0.1')
            # Repeating local sign-out is harmless.
            forget_local_account(private)

    def test_local_session_symlink_does_not_delete_desktop_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / '.private'
            private.mkdir()
            target = root / 'Desktop-key'
            target.write_bytes(b'artificial key')
            (private / 'desktop_123.session').symlink_to(target)
            forget_local_account(private)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), b'artificial key')
            self.assertFalse((private / 'desktop_123.session').is_symlink())

    def test_failed_removal_still_disables_automatic_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp)
            session = private / 'account.session'
            session.write_bytes(b'fake')
            original_unlink = Path.unlink
            def denied(path, *args, **kwargs):
                if path == session:
                    raise PermissionError('test only')
                return original_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', denied):
                with self.assertRaises(PermissionError):
                    forget_local_account(private)
            self.assertFalse(json.loads((private / 'settings.json').read_text())['auto_connect'])

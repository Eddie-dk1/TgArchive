import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors

from telegram_archive.auth import authorize_qr, session_key


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_session_needs_no_qr_phone_or_password(self):
        user = SimpleNamespace(id=42)
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                 get_me=AsyncMock(return_value=user), qr_login=AsyncMock())
        prompt = AsyncMock()
        events = []
        self.assertIs(await authorize_qr(client, lambda *x: events.append(x), prompt), user)
        client.qr_login.assert_not_awaited()
        prompt.assert_not_awaited()
        self.assertEqual(events, [])

    async def test_expired_qr_refreshes_then_logs_in(self):
        user = SimpleNamespace(id=42)
        first = SimpleNamespace(url='tg://login?token=expired', wait=AsyncMock(side_effect=asyncio.TimeoutError))
        second = SimpleNamespace(url='tg://login?token=new', wait=AsyncMock(return_value=user))
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                 qr_login=AsyncMock(side_effect=[first, second]))
        events = []
        self.assertIs(await authorize_qr(client, lambda *x: events.append(x), AsyncMock()), user)
        self.assertEqual([v for k, v in events if k == 'qr'], [first.url, second.url])
        self.assertEqual(events[-1], ('qr_close', None))

    async def test_qr_supports_2fa_and_retries_password(self):
        user = SimpleNamespace(id=42)
        qr = SimpleNamespace(url='tg://login?token=test', wait=AsyncMock(
            side_effect=errors.SessionPasswordNeededError(request=None)))
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                 qr_login=AsyncMock(return_value=qr), sign_in=AsyncMock(
                                     side_effect=[errors.PasswordHashInvalidError(request=None), user]))
        prompt = AsyncMock(side_effect=['wrong', 'correct'])
        self.assertIs(await authorize_qr(client, lambda *x: None, prompt), user)
        self.assertEqual(prompt.await_count, 2)
        client.sign_in.assert_awaited_with(password='correct')

    async def test_cancel_cleans_waiter_and_closes_qr(self):
        started = asyncio.Event()
        ended = asyncio.Event()
        async def wait():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()
        qr = SimpleNamespace(url='tg://login?token=test', wait=wait)
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                 qr_login=AsyncMock(return_value=qr))
        events = []
        task = asyncio.create_task(authorize_qr(client, lambda *x: events.append(x), AsyncMock()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ended.is_set())
        self.assertEqual(events[-1], ('qr_close', None))

    def test_legacy_session_key_reused(self):
        self.assertEqual(session_key({'phone': '+7 (999) 123-45-67'}),
                         session_key({'phone': '+79991234567'}))
        self.assertNotEqual(session_key({'phone': '+79991234567'}), 'account')
        self.assertEqual(session_key({}), 'account')

import asyncio
import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import errors
from telethon.tl import types

from telegram_archive.core import Archive, BLOCK, download_file, export_channel, safe_name


def channel(cid=123):
    return types.Channel(id=cid, title='Приватный <канал>', photo=types.ChatPhotoEmpty(),
                         date=datetime.now(timezone.utc), noforwards=True, broadcast=True)


def message(mid, text='', payload=None, photo=False):
    media = None
    if payload is not None:
        if photo:
            media = types.MessageMediaPhoto(photo=types.Photo(
                id=mid * 10, access_hash=1, file_reference=b'x', date=datetime.now(timezone.utc),
                sizes=[types.PhotoSize(type='x', w=100, h=100, size=len(payload))], dc_id=2))
        else:
            media = types.MessageMediaDocument(document=types.Document(
                id=mid * 10, access_hash=1, file_reference=b'x', date=datetime.now(timezone.utc),
                mime_type='video/mp4', size=len(payload), dc_id=2,
                attributes=[types.DocumentAttributeFilename(file_name='../../video.mp4'),
                            types.DocumentAttributeVideo(duration=1, w=100, h=100)]))
    return types.Message(id=mid, peer_id=types.PeerChannel(123), date=datetime.now(timezone.utc),
                         message=text, media=media, noforwards=True)


class MessageList(list):
    total = 0


class FakeClient:
    def __init__(self, messages, data=None):
        self.messages = messages
        self.data = data or {}
        self.downloads = []
        self.offsets = []
        self.fail = set()
        self.cancel_at = None
        self.flood = False
        self.history_calls = []
        self.lookup_calls = []

    async def get_me(self):
        return types.User(id=9)

    async def get_messages(self, entity, limit=None, ids=None):
        if ids is not None:
            self.lookup_calls.append(ids)
            if isinstance(ids, list):
                return [next((m for m in self.messages if m.id == mid), None) for mid in ids]
            return next((m for m in self.messages if m.id == ids), None)
        result = MessageList(self.messages[-1:] if self.messages else [])
        result.total = len(self.messages)
        return result

    async def iter_messages(self, entity, reverse, min_id, max_id, wait_time):
        self.history_calls.append((min_id, max_id))
        for m in self.messages:
            if min_id < m.id < max_id:
                yield m

    async def iter_download(self, media, offset, request_size, **kwargs):
        doc = getattr(media, 'document', None)
        fid = doc.id if doc else media.id
        self.downloads.append(fid)
        self.offsets.append(offset)
        if self.flood:
            self.flood = False
            raise errors.FloodWaitError(request=None, capture=1)
        if fid in self.fail:
            raise ConnectionError('temporary network problem')
        data = self.data[fid]
        for start in range(offset, len(data), request_size):
            if self.cancel_at is not None and start >= self.cancel_at:
                raise asyncio.CancelledError()
            yield data[start:start + request_size]


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def emit(self, kind, value):
        self.events.append((kind, value))

    async def test_protected_channel_export_edits_dedup_and_missing_file_recovery(self):
        data = b'video contents'
        client = FakeClient([message(1, '=formula <script>alert(1)</script>'),
                             message(2, 'caption', data)], {20: data})
        path = await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(client.downloads, [20])
        records = [json.loads(line) for line in (path / 'messages.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 2)
        file = path / records[1]['path']
        self.assertEqual(file.read_bytes(), data)
        self.assertTrue(file.is_relative_to(path))
        self.assertNotIn('<script>', (path / 'index.html').read_text())
        self.assertIn('&lt;script&gt;', (path / 'index.html').read_text())
        with (path / 'messages.csv').open(encoding='utf-8-sig') as f:
            self.assertTrue(list(csv.reader(f))[1][2].startswith("'=formula"))
        client.messages[0].message = 'edited post'
        await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(client.downloads, [20])
        rows = [json.loads(x) for x in (path / 'messages.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['text'], 'edited post')
        file.unlink()
        await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(client.downloads, [20, 20])
        self.assertEqual(file.read_bytes(), data)

    async def test_cancel_then_resume_partial_file_byte_exact(self):
        data = bytes(range(256)) * 6000
        msg = message(1, payload=data)
        client = FakeClient([msg], {10: data})
        client.cancel_at = BLOCK
        with self.assertRaises(asyncio.CancelledError):
            await export_channel(client, channel(), self.root, self.emit)
        path = self.root / 'channel_-1000000000123'
        partial = list(path.rglob('*.part'))[0]
        self.assertEqual(partial.stat().st_size, BLOCK)
        self.assertFalse(json.loads((path / 'channel.json').read_text())['scan_complete'])
        # Simulate an interrupted chunk that did not reach the request boundary.
        with partial.open('ab') as f:
            f.write(b'broken partial block')
        client.cancel_at = None
        await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(client.offsets[-1], BLOCK)
        record = json.loads((path / 'messages.jsonl').read_text())
        self.assertEqual((path / record['path']).read_bytes(), data)
        self.assertFalse(list(path.rglob('*.part')))

    async def test_failed_old_media_retried_even_when_new_posts_exist(self):
        client = FakeClient([message(1, payload=b'x')], {10: b'x'})
        client.fail.add(10)
        with patch('telegram_archive.core.asyncio.sleep', new=AsyncMock()):
            path = await export_channel(client, channel(), self.root, self.emit)
        row = json.loads((path / 'messages.jsonl').read_text())
        self.assertEqual(row['status'], 'error')
        client.messages.append(message(2, 'new message'))
        client.fail.clear()
        await export_channel(client, channel(), self.root, self.emit)
        rows = [json.loads(x) for x in (path / 'messages.jsonl').read_text().splitlines()]
        self.assertEqual(rows[0]['status'], 'done')
        self.assertEqual(len(rows), 2)

    async def test_flood_wait_retries_same_file(self):
        client = FakeClient([message(1, payload=b'x')], {10: b'x'})
        client.flood = True
        with patch('telegram_archive.core.asyncio.sleep', new=AsyncMock()) as sleep:
            path = await export_channel(client, channel(), self.root, self.emit)
        sleep.assert_awaited_with(2)
        self.assertEqual(client.downloads, [10, 10])
        self.assertEqual(json.loads((path / 'messages.jsonl').read_text())['status'], 'done')

    async def test_photo_download_and_page_boundaries(self):
        client = FakeClient([message(1, payload=b'jpeg', photo=True)] +
                            [message(i, f'post {i}') for i in range(2, 1003)], {10: b'jpeg'})
        path = await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(len((path / 'messages.jsonl').read_text().splitlines()), 1002)
        self.assertTrue((path / 'page_3.html').exists())
        self.assertEqual(sum((path / n).read_text().count('<article ') for n in
                             ('index.html', 'page_2.html', 'page_3.html')), 1002)
        self.assertIn('photos', json.loads((path / 'messages.jsonl').read_text().splitlines()[0])['path'])

    async def test_same_post_ids_isolated_by_channel(self):
        client = FakeClient([message(1, 'post')])
        a = await export_channel(client, channel(123), self.root, self.emit)
        b = await export_channel(client, channel(124), self.root, self.emit)
        self.assertNotEqual(a, b)
        self.assertEqual(len(list(self.root.rglob('messages.jsonl'))), 2)

    async def test_account_mismatch_refused(self):
        archive = Archive(self.root, channel(), 9)
        archive.close()
        with self.assertRaisesRegex(ValueError, 'другим аккаунтом'):
            Archive(self.root, channel(), 10)

    async def test_empty_history_exports_valid_archive(self):
        path = await export_channel(FakeClient([]), channel(), self.root, self.emit)
        self.assertEqual((path / 'messages.jsonl').read_text(), '')
        self.assertIn('0 сообщений', (path / 'index.html').read_text())

    def test_safe_unicode_names(self):
        name = safe_name('../' + 'Я' * 300)
        self.assertNotIn('/', name)
        self.assertLessEqual(len(name.encode()), 160)


if __name__ == '__main__':
    unittest.main()

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_archive import FakeClient, channel, message
from telegram_archive.core import export_channel
from telegram_archive.integrity import file_sha256
from telegram_archive.issues import read_issues


class IntegrityRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'channel_-1000000000123'
        self.events = []

    def emit(self, kind, value):
        self.events.append((kind, value))

    def rows(self):
        return [json.loads(line) for line in (self.archive/'messages.jsonl').read_text().splitlines()]

    def execute(self, query):
        with sqlite3.connect(self.archive/'archive.sqlite3') as connection:
            connection.execute(query)

    async def test_new_download_records_digest_and_standard_manifest(self):
        data = b'original video'
        await export_channel(FakeClient([message(1,payload=data)], {10:data}), channel(), self.root, self.emit)
        row = self.rows()[0]
        self.assertEqual(row['sha256'], hashlib.sha256(data).hexdigest())
        self.assertTrue(row['verified_at'])
        self.assertEqual((self.archive/'SHA256SUMS').read_text(), f'{row["sha256"]}  {row["path"]}\n')

    async def test_same_size_corruption_redownloads_instead_of_recovering_bad_target(self):
        original = b'original video'
        client = FakeClient([message(1,payload=original)], {10:original})
        await export_channel(client, channel(), self.root, self.emit)
        target = self.archive/self.rows()[0]['path']
        target.write_bytes(b'x'*len(original))
        client.history_calls.clear()
        await export_channel(client, channel(), self.root, self.emit, mode='resume')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(client.downloads,[10,10])
        self.assertEqual(client.history_calls,[])
        journal = (self.archive/'journal.jsonl').read_text()
        self.assertIn('checksum_mismatch',journal)
        self.assertEqual(self.rows()[0]['sha256'],hashlib.sha256(original).hexdigest())

    async def test_existing_file_without_digest_gets_honest_baseline(self):
        client=FakeClient([message(1,payload=b'legacy')],{10:b'legacy'})
        await export_channel(client,channel(),self.root,self.emit)
        self.execute('UPDATE messages SET sha256=NULL,verified_at=NULL')
        await export_channel(client,channel(),self.root,self.emit,mode='resume')
        self.assertEqual(client.downloads,[10])
        self.assertEqual(self.rows()[0]['sha256'],hashlib.sha256(b'legacy').hexdigest())
        self.assertIn('Initial baseline', (self.archive/'journal.jsonl').read_text())

    async def test_cancel_during_hashing_does_not_publish_unverified_file(self):
        client=FakeClient([message(1,payload=b'video')],{10:b'video'})
        with patch('telegram_archive.core.file_sha256', new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await export_channel(client,channel(),self.root,self.emit)
        row=self.rows()[0]
        self.assertEqual(row['status'],'pending')
        self.assertIsNone(row['sha256'])
        self.assertEqual(len(list((self.archive/'media').rglob('*.part'))),1)
        await export_channel(client,channel(),self.root,self.emit,mode='resume')
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertEqual(self.rows()[0]['sha256'],hashlib.sha256(b'video').hexdigest())

    async def test_hashing_large_file_can_be_cancelled(self):
        path=self.root/'large.bin'
        path.write_bytes(b'a'*(4*1024*1024))
        task=asyncio.create_task(file_sha256(path))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

    async def test_retry_touches_failed_files_only_and_never_scans_history(self):
        client=FakeClient([message(i,payload=bytes([i])) for i in (1,2,3)],{10:b'\1',20:b'\2',30:b'\3'})
        client.fail.add(20)
        with patch('telegram_archive.core.asyncio.sleep',new=AsyncMock()):
            await export_channel(client,channel(),self.root,self.emit)
        (self.archive/self.rows()[2]['path']).unlink()
        self.execute("UPDATE messages SET status='pending',path=NULL,size=NULL,sha256=NULL WHERE id=3")
        client.fail.clear();client.downloads.clear();client.lookup_calls.clear();client.history_calls.clear()
        await export_channel(client,channel(),self.root,self.emit,mode='retry')
        self.assertEqual(client.downloads,[20])
        self.assertEqual(client.lookup_calls,[[2]])
        self.assertEqual(client.history_calls,[])
        self.assertEqual([r['status'] for r in self.rows()],['done','done','pending'])
        self.assertEqual(read_issues(self.archive)['retryable'],0)
        self.assertEqual(read_issues(self.archive)['total'],1)

    async def test_retry_rechecks_previously_unavailable_attachment(self):
        class Missing(FakeClient):
            missing=True
            async def get_messages(self,*args,**kwargs):
                if kwargs.get('ids') and self.missing: return [None for _ in kwargs['ids']]
                return await super().get_messages(*args,**kwargs)
        client=Missing([message(1,payload=b'video')],{10:b'video'})
        await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(read_issues(self.archive)['retryable'],1)
        client.missing=False
        await export_channel(client,channel(),self.root,self.emit,mode='retry')
        self.assertEqual(client.downloads,[10])
        self.assertEqual(self.rows()[0]['status'],'done')
        final=[v for k,v in self.events if k=='summary'][-1]
        self.assertEqual((final['files_done'],final['files_unavailable'],final['files_pending']),(1,0,0))

    async def test_retry_missing_archive_does_not_create_an_empty_one(self):
        with self.assertRaises(ValueError):
            await export_channel(FakeClient([]),channel(),self.root,self.emit,mode='retry')
        self.assertFalse(self.archive.exists())

    async def test_issue_reader_is_readonly_and_pages_all_pending_files(self):
        client=FakeClient([message(i,payload=b'x') for i in range(1,106)],{i*10:b'x' for i in range(1,106)})
        client.cancel_at=0
        with self.assertRaises(asyncio.CancelledError):
            await export_channel(client,channel(),self.root,self.emit)
        db=self.archive/'archive.sqlite3'
        before=db.read_bytes()
        first=read_issues(self.archive)
        second=read_issues(self.archive,100)
        self.assertEqual((first['total'],len(first['rows']),len(second['rows'])),(105,100,5))
        self.assertEqual(first['retryable'],0)
        self.assertEqual(first['account_id'],9)
        self.assertEqual(before,db.read_bytes())

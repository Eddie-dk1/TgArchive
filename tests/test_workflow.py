import asyncio
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon import errors

from test_archive import FakeClient, channel, message
from telegram_archive.core import Archive, BLOCK, export_channel


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'channel_-1000000000123'
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def emit(self, kind, value):
        self.events.append((kind, value))

    def metadata(self):
        return json.loads((self.path / 'channel.json').read_text())

    def rows(self):
        return [json.loads(x) for x in (self.path / 'messages.jsonl').read_text().splitlines()]

    def journal(self):
        return [json.loads(x) for x in (self.path / 'journal.jsonl').read_text().splitlines()]

    async def test_all_texts_exported_before_first_media_even_if_cancelled(self):
        root = self.path
        class InspectClient(FakeClient):
            async def iter_download(self, *args, **kwargs):
                records = [json.loads(x) for x in (root / 'messages.jsonl').read_text().splitlines()]
                if len(records) != 3:
                    raise AssertionError('History must be exported before media')
                raise asyncio.CancelledError()
                yield b''
        client = InspectClient([message(1, 'caption', b'video'), message(2, 'later text'), message(3, 'last text')])
        with self.assertRaises(asyncio.CancelledError):
            await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual([r['id'] for r in self.rows()], [1,2,3])
        self.assertTrue(self.metadata()['snapshot']['history_complete'])
        self.assertEqual(self.metadata()['last_outcome'], 'cancelled')
        self.assertIn('run_cancelled', [e['event'] for e in self.journal()])

    async def test_resume_reuses_queue_without_history_or_new_post_lookup(self):
        data = b'a' * (BLOCK + 19)
        client = FakeClient([message(1, payload=data), message(2, 'text')], {10:data})
        client.cancel_at = BLOCK
        with self.assertRaises(asyncio.CancelledError):
            await export_channel(client, channel(), self.root, self.emit)
        client.cancel_at = None
        client.messages.append(message(3, 'new post after snapshot'))
        client.history_calls.clear(); client.lookup_calls.clear()
        with patch.object(client, 'iter_messages', side_effect=AssertionError('Resume must not scan history')):
            await export_channel(client, channel(), self.root, self.emit, mode='resume')
        self.assertEqual([r['id'] for r in self.rows()], [1,2])
        self.assertEqual(client.lookup_calls, [[1]])
        self.assertEqual(client.offsets[-1], BLOCK)
        summary = [v for k,v in self.events if k=='summary'][-1]
        self.assertEqual(summary['bytes_transferred'], 19)
        self.assertEqual(summary['done_bytes'], len(data))
        await export_channel(client, channel(), self.root, self.emit, mode='full')
        self.assertEqual([r['id'] for r in self.rows()], [1,2,3])

    async def test_interrupted_history_resumes_at_atomic_cursor(self):
        class Interrupted(FakeClient):
            async def iter_messages(self, entity, reverse, min_id, max_id, wait_time):
                async for m in super().iter_messages(entity,reverse,min_id,max_id,wait_time):
                    yield m
                    if m.id == 2:
                        raise asyncio.CancelledError()
        messages = [message(i, str(i)) for i in range(1,6)]
        with self.assertRaises(asyncio.CancelledError):
            await export_channel(Interrupted(messages), channel(), self.root, self.emit)
        state = self.metadata()['snapshot']
        self.assertEqual(state['cursor'], 2)
        self.assertFalse(state['history_complete'])
        client = FakeClient(messages)
        await export_channel(client, channel(), self.root, self.emit, mode='resume')
        self.assertEqual(client.history_calls, [(2,6)])
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(self.metadata()['snapshot']['id'], state['id'])

    async def test_errors_and_retries_survive_successful_next_run(self):
        client = FakeClient([message(1, payload=b'x')], {10:b'x'})
        client.fail.add(10)
        with patch('telegram_archive.core.asyncio.sleep', new=AsyncMock()):
            await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(self.rows()[0]['attempts'], 3)
        self.assertIn('temporary network problem', self.rows()[0]['error'])
        events = self.journal()
        self.assertEqual(sum(e['event']=='download_error' for e in events), 3)
        self.assertEqual(sum(e['event']=='download_retry' for e in events), 2)
        with (self.path / 'errors.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)
        client.fail.clear()
        await export_channel(client, channel(), self.root, self.emit, mode='resume')
        self.assertEqual(self.rows()[0]['attempts'], 4)
        self.assertEqual(self.rows()[0]['status'], 'done')
        self.assertEqual(sum(e['event']=='download_error' for e in self.journal()), 3)
        self.assertEqual(self.metadata()['progress']['files_errors'], 0)
        self.assertIn('temporary network problem', (self.path / 'report.html').read_text())

    async def test_deleted_queued_post_is_unavailable_not_generic_failure(self):
        class Removed(FakeClient):
            async def get_messages(self, entity, limit=None, ids=None):
                return [None for _ in ids] if isinstance(ids,list) else await super().get_messages(entity,limit,ids)
        client = Removed([message(1, payload=b'x')], {10:b'x'})
        await export_channel(client, channel(), self.root, self.emit)
        row=self.rows()[0]
        self.assertEqual(row['status'], 'unavailable')
        self.assertEqual(row['availability'], 'unavailable')
        self.assertEqual(client.downloads, [])
        self.assertEqual(self.metadata()['progress']['files_unavailable'], 1)

    async def test_gap_verification_distinguishes_unavailable_from_recovered(self):
        class MissingInHistory(FakeClient):
            async def iter_messages(self, *args, **kwargs):
                async for m in super().iter_messages(*args, **kwargs):
                    if m.id != 2: yield m
        client=MissingInHistory([message(1),message(2,'recovered'),message(4)])
        await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual([r['id'] for r in self.rows()], [1,2,4])
        self.assertEqual(self.metadata()['id_gaps'], {'unavailable':1,'unverified':0})
        self.assertIn('gap_recovered', [e['event'] for e in self.journal()])
        self.assertEqual(self.metadata()['progress']['history_seen'],3)

    async def test_enormous_id_gap_is_bounded_and_reported_honestly(self):
        client = FakeClient([message(1),message(1000000000)])
        with patch('telegram_archive.core.MAX_GAP_CHECK_IDS',3):
            await export_channel(client, channel(), self.root, self.emit)
        self.assertEqual(client.lookup_calls, [[2,3,4]])
        self.assertEqual(self.metadata()['id_gaps'], {'unavailable':3,'unverified':999999995})

    async def test_flood_wait_written_with_duration(self):
        client = FakeClient([message(1, payload=b'x')], {10:b'x'})
        client.flood=True
        with patch('telegram_archive.core.asyncio.sleep',new=AsyncMock()):
            await export_channel(client, channel(), self.root, self.emit)
        events=[e for e in self.journal() if e['event']=='flood_wait']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['message_id'],1)
        self.assertEqual(events[0]['delay_seconds'],1)

    async def test_missing_local_file_is_repaired_in_resume_mode(self):
        client=FakeClient([message(1,payload=b'x')],{10:b'x'})
        await export_channel(client,channel(),self.root,self.emit)
        (self.path/self.rows()[0]['path']).unlink()
        client.history_calls.clear()
        await export_channel(client,channel(),self.root,self.emit,mode='resume')
        self.assertEqual(client.history_calls,[])
        self.assertEqual(client.downloads,[10,10])
        self.assertIn('local_file_requeued',[e['event'] for e in self.journal()])

    async def test_full_scan_preserves_downloaded_post_removed_from_telegram(self):
        client=FakeClient([message(1,payload=b'x'),message(2)],{10:b'x'})
        await export_channel(client,channel(),self.root,self.emit)
        saved=self.path/self.rows()[0]['path']
        client.messages=[]
        await export_channel(client,channel(),self.root,self.emit,mode='full')
        self.assertEqual(self.rows()[0]['availability'],'unavailable')
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertEqual(saved.read_bytes(),b'x')

    async def test_completed_target_after_crash_does_not_download_again(self):
        client=FakeClient([message(1,payload=b'x')],{10:b'x'})
        await export_channel(client,channel(),self.root,self.emit)
        con=sqlite3.connect(self.path/'archive.sqlite3')
        con.execute("UPDATE messages SET status='pending', path=NULL, size=NULL")
        con.commit();con.close()
        await export_channel(client,channel(),self.root,self.emit,mode='resume')
        self.assertEqual(client.downloads,[10])
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertIn('completed_file_recovered',[e['event'] for e in self.journal()])

    async def test_disk_full_keeps_queue_and_exact_error(self):
        client=FakeClient([message(1,payload=b'x'),message(2,'later')],{10:b'x'})
        with patch('telegram_archive.core.shutil.disk_usage', return_value=type('Disk',(),{'free':0})()):
            with self.assertRaises(OSError):
                await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(len(self.rows()),2)
        self.assertIn('Недостаточно места',self.rows()[0]['error'])
        self.assertEqual(self.metadata()['last_outcome'],'failed')

    async def test_file_reference_refresh_and_retry(self):
        class Expired(FakeClient):
            expired=True
            async def iter_download(self,*args,**kwargs):
                if self.expired:
                    self.expired=False
                    raise errors.FileReferenceExpiredError(request=None)
                async for chunk in super().iter_download(*args,**kwargs):yield chunk
        client=Expired([message(1,payload=b'x')],{10:b'x'})
        await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertIn('file_reference_expired',[e['event'] for e in self.journal()])

    async def test_single_writer_lock(self):
        first=Archive(self.root,channel(),9)
        try:
            with self.assertRaisesRegex(ValueError,'уже обрабатывается'):
                Archive(self.root,channel(),9)
        finally:first.close()

    async def test_legacy_database_migrated_without_redownloading_media(self):
        self.path.mkdir()
        payload=b'legacy video'
        m=message(1,'legacy caption',payload)
        media=self.path/'media/videos/legacy.mp4';media.parent.mkdir(parents=True);media.write_bytes(payload)
        db=sqlite3.connect(self.path/'archive.sqlite3')
        db.executescript('''CREATE TABLE messages(id INTEGER PRIMARY KEY,date TEXT,text TEXT,category TEXT,
            media_key TEXT,path TEXT,size INTEGER,status TEXT,error TEXT,link TEXT,raw TEXT NOT NULL);
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);''')
        from telegram_archive.core import dumps
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (1,m.date.isoformat(),m.message,'videos','videos:10','media/videos/legacy.mp4',len(payload),
                    'done',None,'https://t.me/c/123/1',dumps(m.to_dict())))
        db.execute("INSERT INTO metadata VALUES('account_id','9')");db.commit();db.close()
        client=FakeClient([m],{10:payload})
        await export_channel(client,channel(),self.root,self.emit,mode='resume')
        self.assertEqual(client.downloads,[])
        self.assertEqual(media.read_bytes(),payload)
        self.assertEqual(self.rows()[0]['expected_size'],len(payload))
        self.assertEqual(self.metadata()['schema_version'],2)
        self.assertEqual(len(client.history_calls),1)

    async def test_progress_reports_whole_snapshot_and_exact_bytes(self):
        client=FakeClient([message(1,payload=b'ab'),message(2,payload=b'cde')],{10:b'ab',20:b'cde'})
        await export_channel(client,channel(),self.root,self.emit)
        summaries=[v for k,v in self.events if k=='summary']
        self.assertTrue(any(s['phase']=='history' for s in summaries))
        final=summaries[-1]
        self.assertEqual((final['files_total'],final['files_done'],final['files_pending']),(2,2,0))
        self.assertEqual((final['total_bytes'],final['done_bytes'],final['bytes_transferred']),(5,5,5))
        self.assertEqual(final['history_seen'],2)

    async def test_empty_document_counts_as_completed_file(self):
        client=FakeClient([message(1,payload=b'')],{10:b''})
        await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(self.rows()[0]['status'],'done')
        self.assertEqual(self.metadata()['progress']['files_done'],1)

    async def test_gap_check_can_resume_after_stop_without_rechecking_ids(self):
        client=FakeClient([message(1),message(5)])
        with patch('telegram_archive.core.BATCH_SIZE',1), patch('telegram_archive.core.asyncio.sleep',new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(client.lookup_calls,[[2]])
        self.assertFalse(self.metadata()['snapshot']['gaps_complete'])
        client.lookup_calls.clear()
        await export_channel(client,channel(),self.root,self.emit,mode='resume')
        self.assertEqual(client.lookup_calls,[[3,4]])
        self.assertEqual(self.metadata()['id_gaps'],{'unavailable':3,'unverified':0})

    async def test_posts_newer_than_snapshot_wait_for_full_scan(self):
        class Growing(FakeClient):
            async def get_messages(self,*args,**kwargs):
                result=await super().get_messages(*args,**kwargs)
                if kwargs.get('limit')==1 and len(self.messages)==2:
                    self.messages.append(message(3,'new after snapshot'))
                return result
        client=Growing([message(1),message(2)])
        await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual([r['id'] for r in self.rows()],[1,2])
        self.assertEqual(self.metadata()['snapshot']['upper_id'],2)

    async def test_unavailable_gap_api_does_not_prevent_media_downloads(self):
        class Unsupported(FakeClient):
            async def get_messages(self,entity,limit=None,ids=None):
                if ids and 2 in ids:
                    raise errors.ChatAdminRequiredError(request=None)
                return await super().get_messages(entity,limit,ids)
        client=Unsupported([message(1,payload=b'x'),message(3)],{10:b'x'})
        await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(client.downloads,[10])
        self.assertEqual(self.metadata()['id_gaps'],{'unavailable':0,'unverified':1})

    async def test_vanished_latest_snapshot_post_is_counted_as_gap(self):
        class Vanished(FakeClient):
            async def iter_messages(self,*args,**kwargs):
                async for m in super().iter_messages(*args,**kwargs):
                    if m.id != 2: yield m
            async def get_messages(self,entity,limit=None,ids=None):
                if ids == [2]:return [None]
                return await super().get_messages(entity,limit,ids)
        client=Vanished([message(1),message(2)])
        await export_channel(client,channel(),self.root,self.emit)
        self.assertEqual(self.metadata()['id_gaps'],{'unavailable':1,'unverified':0})

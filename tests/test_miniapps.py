"""Mini App integration fixtures exercise actual HTTP streaming and durable state."""
import asyncio
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from telegram_archive.miniapps import Asset, Catalogue, MiniAppError, Node, identity
from telegram_archive.miniapps.archive import archive
from telegram_archive.miniapps.edify import Edify, clean_markdown, file_reference, lesson_assets, validate_url
from telegram_archive.miniapps.store import Store


VIDEO = b'\0\0\0\x18ftypisom' + bytes(range(256)) * 4096
PHOTO = b'\xff\xd8\xff\xe0fixture-photo\xff\xd9'


def catalogue(photo=True):
    nodes = [Node('course:1','','course','Курс <script>'), Node('module:1','course:1','module','Первый раздел'),
             Node('lesson:1','module:1','lesson','Тестовый урок',text='Текст урока. <script>alert(1)</script>'),
             Node('module:2','course:1','module','Будущий раздел',1,state='pending')]
    assets = [Asset('lesson:1','video:0','movie.mp4','video',{'type':'video','id':'v'},identity('v'),len(VIDEO),True)]
    if photo:
        assets.append(Asset('lesson:1','photo:0','photo.jpg','photo',{'type':'key','key':'photo'},identity('photo'),len(PHOTO)))
    return Catalogue(nodes, assets)


class Stream(httpx.AsyncByteStream):
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail

    async def __aiter__(self):
        for offset in range(0, len(self.data), 64 * 1024):
            yield self.data[offset:offset+64*1024]
            await asyncio.sleep(0)
            if self.fail:
                raise httpx.ReadError('connection lost https://cdn.example/file?secret=signed-secret')


class Provider:
    source, account_id, bot_id = 'edify', 123, 456

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or catalogue()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle), follow_redirects=False)
        self.requests, self.resolves = [], 0
        self.ignore_range = False
        self.expire_once = False
        self.bad_range = False
        self.fail_stream = False
        self.piece_size = None

    async def catalogue(self):
        return self.snapshot

    async def resolve(self, asset):
        self.resolves += 1
        return 'https://cdn.example/' + ('photo' if asset.kind == 'photo' else 'video') + '?secret=signed-secret'

    def handle(self, request):
        self.requests.append(request)
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(403)
        data = PHOTO if request.url.path == '/photo' else VIDEO
        offset = 0
        headers = {'content-type':'image/jpeg' if request.url.path == '/photo' else 'video/mp4'}
        status = 200
        if request.headers.get('range') and not self.ignore_range:
            offset = int(request.headers['range'].removeprefix('bytes=').removesuffix('-'))
            status = 206
            headers['content-range'] = f'bytes {offset+1 if self.bad_range else offset}-{len(data)-1}/{len(data)}'
        end = min(len(data),offset+self.piece_size) if self.piece_size else len(data)
        if self.piece_size:
            status=206
            headers['content-range'] = f'bytes {offset}-{end-1}/{len(data)}'
        headers['content-length'] = str(end-offset)
        return httpx.Response(status,headers=headers,stream=Stream(data[offset:end], self.fail_stream))


class ArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.provider = Provider()
        self.addAsyncCleanup(self.provider.http.aclose)
        # Keep retry tests fast; zero-delay cooperative yields still work.
        original = asyncio.sleep
        async def fast(delay):
            await original(0)
        patcher = patch('telegram_archive.miniapps.download.asyncio.sleep', new=fast)
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self, root, sql='SELECT * FROM assets ORDER BY rowid'):
        with sqlite3.connect(root/'catalogue.sqlite3') as connection:
            connection.row_factory=sqlite3.Row
            return connection.execute(sql).fetchall()

    async def test_download_text_photo_video_offline_and_no_secret_leaks(self):
        result = await archive(self.provider,self.output)
        self.assertEqual(result['counts']['done'],2)
        self.assertEqual(result['pending_modules'],1)
        rows = self.read(result['root'])
        for row,data in zip(rows,(VIDEO,PHOTO)):
            self.assertEqual((result['root']/row['path']).read_bytes(),data)
            self.assertEqual(row['sha256'],hashlib.sha256(data).hexdigest())
        page = (result['root']/'index.html').read_text()
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',page)
        self.assertNotIn('<script>',page)
        self.assertIn('<video controls',page)
        self.assertIn('<img ',page)
        texts = list(result['root'].rglob('lesson.md'))
        self.assertTrue(any('Текст урока.' in p.read_text() for p in texts))
        for path in result['root'].rglob('*'):
            if path.is_file():
                self.assertNotIn(b'signed-secret',path.read_bytes(),str(path))
        self.assertTrue(all('authorization' not in r.headers for r in self.provider.requests))

    async def stop_mid_file(self):
        def stop(kind,value):
            if kind == 'progress' and value[1] >= 256*1024:
                asyncio.current_task().cancel()
        task = asyncio.create_task(archive(self.provider,self.output,stop))
        with self.assertRaises(asyncio.CancelledError):
            await task
        root = next(self.output.glob('miniapp_*'))
        parts = list(root.rglob('*.part'))
        self.assertEqual(len(parts),1)
        self.assertGreater(parts[0].stat().st_size,0)
        self.assertLess(parts[0].stat().st_size,len(VIDEO))
        return root,parts[0].stat().st_size

    async def test_stop_restart_resumes_exact_range_and_repeat_reuses_files(self):
        root,size = await self.stop_mid_file()
        self.provider.requests.clear()
        result = await archive(self.provider,self.output)
        self.assertEqual(self.provider.requests[0].headers['range'],f'bytes={size}-')
        self.assertEqual(result['counts']['done'],2)
        self.assertEqual((root/self.read(root)[0]['path']).read_bytes(),VIDEO)
        self.provider.requests.clear()
        again = await archive(self.provider,self.output)
        self.assertEqual(again['downloaded'],0)
        self.assertEqual(self.provider.requests,[])
        states = self.read(root,'SELECT state FROM runs ORDER BY id')
        self.assertEqual([r['state'] for r in states],['cancelled','finished','finished'])

    async def test_ignored_range_restarts_without_appending(self):
        root,_ = await self.stop_mid_file()
        self.provider.ignore_range = True
        await archive(self.provider,self.output)
        self.assertEqual((root/self.read(root)[0]['path']).read_bytes(),VIDEO)

    async def test_server_caps_ranges_into_many_successful_pieces(self):
        self.provider.snapshot=catalogue(photo=False)
        self.provider.piece_size=128*1024
        result=await archive(self.provider,self.output)
        self.assertEqual(result['counts']['done'],1)
        self.assertEqual(result['counts']['error'],0)
        self.assertGreater(len(self.provider.requests),3)
        self.assertEqual(self.provider.requests[1].headers['range'],'bytes=131072-')
        self.assertEqual((result['root']/self.read(result['root'])[0]['path']).read_bytes(),VIDEO)

    async def test_invalid_content_range_never_marks_partial_done(self):
        root,_ = await self.stop_mid_file()
        self.provider.bad_range = True
        result = await archive(self.provider,self.output)
        self.assertEqual(result['counts']['error'],1)
        self.assertNotEqual(self.read(root)[0]['status'],'done')
        self.assertIn('Файл не объединён',self.read(root)[0]['error'])

    async def test_expiring_url_is_resolved_again(self):
        self.provider.expire_once = True
        result = await archive(self.provider,self.output)
        self.assertEqual(result['counts']['done'],2)
        self.assertGreater(self.provider.resolves,2)

    async def test_network_interruption_retains_part_and_sanitizes_error(self):
        self.provider.fail_stream = True
        result = await archive(self.provider,self.output)
        self.assertGreater(result['counts']['error'],0)
        self.assertTrue(list(result['root'].rglob('*.part')))
        self.assertNotIn('signed-secret',(result['root']/'report.json').read_text())
        self.provider.fail_stream = False
        again = await archive(self.provider,self.output)
        self.assertEqual(again['counts']['done'],2)

    async def test_local_same_size_corruption_is_restored(self):
        result = await archive(self.provider,self.output)
        file = result['root']/self.read(result['root'])[0]['path']
        original = file.read_bytes()
        file.write_bytes(b'X'+original[1:])
        self.provider.requests.clear()
        again = await archive(self.provider,self.output)
        self.assertEqual(again['downloaded'],1)
        self.assertEqual(file.read_bytes(),original)

    async def test_changed_remote_identity_does_not_append_old_part(self):
        root,_ = await self.stop_mid_file()
        old_part = list(root.rglob('*.part'))[0]
        self.provider.snapshot.assets[0].fingerprint = identity('new-video')
        self.provider.requests.clear()
        result = await archive(self.provider,self.output)
        self.assertNotIn('range',self.provider.requests[0].headers)
        self.assertTrue(old_part.exists())
        self.assertEqual(result['counts']['done'],2)

    async def test_future_module_unlock_and_text_update_do_not_redownload_video(self):
        first = await archive(self.provider,self.output)
        self.provider.snapshot.nodes[-1].state = 'available'
        self.provider.snapshot.nodes.append(Node('lesson:2','module:2','lesson','Новый урок',text='Новое содержание'))
        self.provider.snapshot.nodes[2].text = 'Обновлённое содержание'
        self.provider.snapshot.assets.append(Asset('lesson:2','photo:1','new.jpg','photo',{'type':'key','key':'new'},identity('new'),len(PHOTO)))
        self.provider.requests.clear()
        second = await archive(self.provider,self.output)
        self.assertEqual(second['downloaded'],1)
        self.assertEqual(second['pending_modules'],0)
        self.assertEqual(second['counts']['done'],3)
        self.assertIn('Обновлённое содержание',(first['root']/'index.html').read_text())

    async def test_partial_catalogue_retains_saved_text_and_reports_incomplete(self):
        first = await archive(self.provider,self.output)
        snapshot = self.provider.snapshot
        snapshot.complete = False
        snapshot.warnings.append('Не удалось прочитать раздел')
        snapshot.nodes[2].state = 'incomplete'
        snapshot.nodes[2].text = ''
        snapshot.assets.clear()
        again = await archive(self.provider,self.output)
        self.assertFalse(again['catalogue_complete'])
        page = (first['root']/'index.html').read_text()
        self.assertIn('Текст урока.',page)
        self.assertIn('Архив не завершён',page)
        self.assertTrue(all(r['status']=='done' for r in self.read(first['root'])))

    async def test_lost_entitlement_preserves_materials(self):
        result = await archive(self.provider,self.output)
        self.provider.snapshot = Catalogue()
        again = await archive(self.provider,self.output)
        self.assertEqual(again['counts']['done'],0)
        self.assertIn('Ранее сохранённое',(result['root']/'index.html').read_text())
        self.assertTrue(all((result['root']/r['path']).exists() for r in self.read(result['root'])))

    async def test_disk_space_failure_keeps_queue_and_makes_no_requests(self):
        with patch('telegram_archive.miniapps.download.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            result = await archive(self.provider,self.output)
        self.assertEqual(result['counts']['error'],2)
        self.assertEqual(self.provider.requests,[])

    async def test_unsupported_asset_is_visible(self):
        self.provider.snapshot.assets[0].problem = 'Внешнее видео пока не поддерживается.'
        result = await archive(self.provider,self.output)
        self.assertEqual(result['counts']['unsupported'],1)
        self.assertIn('Внешнее видео',(result['root']/'index.html').read_text())

    async def test_cancel_during_hash_does_not_publish_unverified_file(self):
        self.provider.snapshot=catalogue(photo=False)
        with patch('telegram_archive.miniapps.download.file_sha256',new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await archive(self.provider,self.output)
        root=next(self.output.glob('miniapp_*'))
        row=self.read(root)[0]
        self.assertFalse((root/row['path']).exists())
        self.assertEqual(Path(str(root/row['path'])+'.part').stat().st_size,len(VIDEO))
        self.provider.requests.clear()
        result=await archive(self.provider,self.output)
        self.assertEqual(result['counts']['done'],1)
        self.assertEqual(self.provider.requests,[])

    async def test_missing_finished_file_is_downloaded_again(self):
        first=await archive(self.provider,self.output)
        row=self.read(first['root'])[0]
        (first['root']/row['path']).unlink()
        again=await archive(self.provider,self.output)
        self.assertEqual(again['downloaded'],1)
        self.assertEqual((first['root']/row['path']).read_bytes(),VIDEO)


class SchemaTests(unittest.TestCase):
    def test_multiple_video_legacy_dedup_and_empty_array(self):
        video = dict(kind='telegram',fileId='file',chatId='1',messageId=1,sizeBytes=50)
        lesson = dict(id='1',video=video,videos=[video,dict(video,fileId='file2')])
        self.assertEqual(len(lesson_assets('lesson:1',lesson)),2)
        lesson['videos'] = []
        self.assertEqual(lesson_assets('lesson:1',lesson),[])
        del lesson['videos']
        self.assertEqual(len(lesson_assets('lesson:1',lesson)),1)

    def test_slider_markdown_photos_and_presentations(self):
        lesson = dict(id='1',contentMarkdown='![фото](https://api.edify.su/files/public?key=pic.jpg)',
                      slider={'images':[{'key':'slider.jpg'}]},
                      presentation={'pdfKey':'presentation.pdf','pptxKey':'presentation.pptx','originalFilename':'file.pptx'})
        assets = lesson_assets('lesson:1',lesson)
        self.assertEqual([a.kind for a in assets],['photo','photo','attachment','attachment'])
        self.assertEqual(assets[1].locator,{'type':'key','key':'pic.jpg'})
        self.assertIn('edify-file:',clean_markdown(lesson['contentMarkdown']))
        self.assertNotIn('secret',clean_markdown('Link https://cdn.example/file?secret=abc'))

    def test_reject_non_https_and_private_addresses(self):
        for url in ('http://example.org/a','file:///tmp/a','https://127.0.0.1/a','https://localhost/a','https://user:pass@example.org/a'):
            with self.subTest(url=url),self.assertRaises(MiniAppError):
                validate_url(url)

    def test_public_photo_keys_use_specific_edify_endpoints(self):
        self.assertEqual(file_reference('/public/course-cover?key=cover.jpg'),
                         dict(type='public_key',path='/public/course-cover',key='cover.jpg'))
        assets=lesson_assets('lesson:l',dict(id='l',slider={'images':[{'key':'pic.jpg'}]}))
        self.assertEqual(assets[0].locator,dict(type='public_key',path='/public/lesson-media',key='pic.jpg'))

    def test_owner_guard_lock_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp,'edify',123,456)
            try:
                with self.assertRaises(MiniAppError):
                    Store(tmp,'edify',123,456)
                outside = Path(tmp)/'outside'
                outside.mkdir()
                (store.root/'link').symlink_to(outside,target_is_directory=True)
                with self.assertRaises(MiniAppError):
                    store.path('link/file')
                with self.assertRaises(MiniAppError):
                    store.path('../outside/file')
                store.meta('account_id','789')
                store.db.commit()
            finally:
                store.close()
            with self.assertRaises(MiniAppError):
                Store(tmp,'edify',123,456)


class EdifyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_login_uses_main_webview_and_only_auth_post(self):
        telegram=AsyncMock()
        telegram.get_me.return_value=SimpleNamespace(id=123)
        telegram.get_input_entity.return_value=SimpleNamespace(user_id=456)
        telegram.return_value=SimpleNamespace(url='https://app.edify.su/#tgWebAppData=memory-init-data')
        def handle(request):
            self.assertEqual(request.method,'POST')
            self.assertEqual(request.url.path,'/auth/telegram')
            self.assertEqual(json.loads(request.content),{'initData':'memory-init-data'})
            self.assertNotIn('authorization',request.headers)
            return httpx.Response(200,json={'accessToken':'memory-token'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            provider=Edify(telegram,http)
            await provider.login()
            self.assertEqual((provider.account_id,provider.bot_id),(123,456))
            self.assertEqual(provider.token,'memory-token')
        telegram.log_out.assert_not_called()

    async def test_changed_video_is_rejected_before_stream_url_is_requested(self):
        lesson=dict(id='l',videos=[dict(kind='telegram',fileId='old',chatId='c',messageId=1,sizeBytes=100)])
        asset=lesson_assets('lesson:l',lesson)[0]
        lesson['videos'][0]['fileId']='new'
        paths=[]
        def handle(request):
            paths.append(request.url.path)
            return httpx.Response(200,json={'lesson':lesson})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            provider=Edify(None,http)
            provider.token='token'
            with self.assertRaises(MiniAppError):
                await provider.resolve(asset)
        self.assertEqual(paths,['/lessons/l'])

    async def test_catalogue_empty_future_modules_and_read_only_contract(self):
        calls = []
        def handle(request):
            path=request.url.path
            calls.append((request.method,path))
            self.assertEqual(request.headers['authorization'],'Bearer memory-only-token')
            responses = {
                '/me/courses': {'items':[{'course':{'id':'c','title':'Course'}}]},
                '/courses/c': {'lessons':[{'id':'l','title':'Lesson','order':1}],'unlockedLessonIds':['l']},
                '/courses/c/modules':{'items':[{'id':'m1','title':'Open','position':1},{'id':'m2','title':'Later','position':2}]},
                '/courses/c/modules/m1/lessons':{'items':[{'id':'l','title':'Lesson','order':1}],'unlockedLessonIds':['l']},
                '/courses/c/modules/m2/lessons':{'items':[],'unlockedLessonIds':[]},
                '/lessons/l':{'lesson':{'id':'l','title':'Lesson','contentMarkdown':'Hello','videos':[]}},
                '/lessons/l/materials':{'items':[{'id':'a','fileKey':'attachment.pdf','filename':'note.pdf','sizeBytes':5}]},
                '/courses/c/podcasts/course-level':{'items':[]},
            }
            return httpx.Response(200,json=responses[path])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            provider = Edify(None,http)
            provider.token='memory-only-token'
            result = await provider.catalogue()
        self.assertTrue(result.complete)
        self.assertEqual(sum(n.state=='pending' for n in result.nodes),1)
        self.assertEqual(sum(n.kind=='lesson' for n in result.nodes),1)
        self.assertEqual(len(result.assets),1)
        self.assertTrue(all(method=='GET' for method,_ in calls))
        self.assertFalse(any('/assignment' in path or '/complete' in path or '/progress' in path for _,path in calls))

    async def test_malformed_api_list_is_not_silently_empty(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={}))) as http:
            provider = Edify(None,http)
            provider.token='token'
            with self.assertRaises(MiniAppError):
                await provider.catalogue()

    async def test_no_courses_prompts_to_check_account_without_a_success_result(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'items':[]}))) as http:
            provider=Edify(None,http)
            provider.token='token'
            result=await provider.catalogue()
        self.assertFalse(result.complete)
        self.assertIn('аккаунт',result.warnings[0])

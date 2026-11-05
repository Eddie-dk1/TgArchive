"""PyInstaller entry point and an isolated, offline bundle smoke check."""
import json
import os
import sys
import traceback
from pathlib import Path

from telegram_archive import __version__


def smoke_test():
    result_path = Path(sys.argv[sys.argv.index('--smoke-result')+1])
    def stage(name):
        result_path.write_text(json.dumps({'ok':False,'stage':name}))
    stage('imports')
    import sqlite3
    from opentele2.td import TDesktop
    from opentele2.tl import TelegramClient
    from python_socks.async_.asyncio import Proxy
    from telegram_archive import gui
    if not os.environ.get('TG_ARCHIVE_DATA_DIR'):
        raise ValueError('Smoke checks require an isolated TG_ARCHIVE_DATA_DIR.')
    gui.PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    gui.App._load = lambda self: None
    gui.find_tdata = lambda: []
    stage('create Tk window')
    app = gui.App()
    try:
        stage('render Tk window')
        app.update()
        stage('render QR')
        app._show_qr('tg://login?token=offline-packaging-test')
        app.update()
        app._close_qr()
        folder = gui.PRIVATE / 'channel_-100123'
        folder.mkdir(exist_ok=True)
        with sqlite3.connect(folder/'archive.sqlite3') as connection:
            connection.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO metadata VALUES('account_id','123');
                CREATE TABLE messages(id INTEGER,date TEXT,category TEXT,status TEXT,error TEXT,attempts INTEGER,link TEXT);
                INSERT INTO messages VALUES(1,'','videos','error','Offline smoke test',3,'https://t.me/c/123/1');''')
        app.archive_path = folder
        stage('open issue window')
        app._show_issues()
        app.update()
        assert app.issues_data['retryable'] == 1
        assert not app.retry_button.instate(['disabled'])
        stage('Mini App HTTP and SOCKS imports')
        import asyncio
        import httpx
        from types import SimpleNamespace
        from telegram_archive.miniapps import Asset, Catalogue, Node, identity
        from telegram_archive.miniapps.archive import archive
        from telegram_archive.miniapps.edify import tls_context
        async def mini_smoke():
            async with httpx.AsyncClient(verify=tls_context(),proxy='socks5://127.0.0.1:1'):
                pass
            async def catalogue():
                return Catalogue([Node('course:smoke','','course','Offline course'),
                    Node('lesson:smoke','course:smoke','lesson','Offline lesson',text='Saved text')],
                    [Asset('lesson:smoke','photo:0','sample.jpg','photo',{},identity('smoke'),4)])
            async def resolve(asset):
                return 'https://example.org/photo'
            class Bytes(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'test'
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request:httpx.Response(200,stream=Bytes(),headers={'content-type':'image/jpeg'}))) as http:
                provider=SimpleNamespace(source='edify',account_id=123,bot_id=456,http=http,
                                         catalogue=catalogue,resolve=resolve)
                return await archive(provider,gui.PRIVATE/'mini-smoke')
        report=asyncio.run(mini_smoke())
        assert report['counts']['done']==1
        stage('Mini App source and report UI')
        app.connected=True
        app.current_account_id=123
        app.source_mode.set('Мини-приложения')
        app._source_changed()
        app.archive_path=report['root']
        app._show_mini_report()
        app.update()
        result = dict(ok=True, version=__version__, tk=app.tk.call('info','patchlevel'),
                      checks=['Tk window','QR rendering','Desktop import modules','SOCKS5 modules','issue window',
                              'Mini App HTTP/SOCKS/TLS','offline Mini App download','Mini App UI'])
        if '--smoke-result' in sys.argv:
            path = Path(sys.argv[sys.argv.index('--smoke-result')+1])
            path.write_text(json.dumps(result, indent=2))
    finally:
        for callback in app.tk.call('after','info'):
            app.after_cancel(callback)
        app.destroy()


if __name__ == '__main__':
    if '--smoke-test' in sys.argv:
        try:
            smoke_test()
        except BaseException:
            if '--smoke-result' in sys.argv:
                Path(sys.argv[sys.argv.index('--smoke-result')+1]).write_text(
                    json.dumps({'ok':False,'error':traceback.format_exc()},indent=2))
            sys.exit(1)
    else:
        from telegram_archive.gui import main
        main()

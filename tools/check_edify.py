"""Explicit live acceptance checks; reads Desktop in memory without logging out.

Not part of the unit suite. Downloaded materials and local test reports must not
be committed. Run from the repository with its virtual environment.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from opentele2.api import UseCurrentSession
from opentele2.tl import TelegramClient
from telethon.sessions import MemorySession

from telegram_archive.desktop import find_tdata, load_tdata
from telegram_archive.miniapps.archive import archive
from telegram_archive.miniapps.edify import APP, Edify, tls_context


async def main(args):
    paths = find_tdata()
    if not paths:
        raise SystemExit('No Telegram Desktop session found.')
    desktop = load_tdata(paths[0])
    if args.account_id is not None:
        account = next((a for a in desktop.accounts if int(a.UserId) == args.account_id), None)
        if account is None:
            raise SystemExit('The archive account is not signed in to Telegram Desktop.')
    else:
        account = desktop.mainAccount if args.account is None else desktop.accounts[args.account - 1]
    telegram = await TelegramClient.FromTDesktop(account, session=MemorySession(), flag=UseCurrentSession,
                                                auto_post_login=False, receive_updates=False)
    try:
        print('Connecting Telegram account for archive',flush=True)
        await telegram.connect()
        last_bytes = 0
        def emit(kind,value):
            nonlocal last_bytes
            if kind == 'mini_summary':
                print(json.dumps(value,ensure_ascii=False),flush=True)
            elif kind == 'log':
                print(value,flush=True)
            elif kind == 'status':
                print(value,flush=True)
            elif kind == 'progress':
                if args.stop_after_mib and value[1] >= args.stop_after_mib * 1024 * 1024:
                    raise asyncio.CancelledError()
                if value[1] - last_bytes > 16 * 1024 * 1024:
                    last_bytes = value[1]
                    print('Transferred MiB:',value[1]//(1024*1024),flush=True)
        async with httpx.AsyncClient(verify=tls_context(), follow_redirects=False,
                                    timeout=httpx.Timeout(45,connect=20),trust_env=False) as http:
            provider = Edify(telegram,http,emit)
            print('Opening Edify authorization',flush=True)
            await provider.login()
            print('Edify authorization ready',flush=True)
            if args.inspect:
                snapshot = await provider.catalogue()
                print(json.dumps(dict(courses=sum(n.kind=='course' for n in snapshot.nodes),
                    modules=sum(n.kind=='module' for n in snapshot.nodes),
                    lessons=sum(n.kind=='lesson' for n in snapshot.nodes),
                    videos=sum(a.kind=='video' for a in snapshot.assets),
                    photos=sum(a.kind=='photo' for a in snapshot.assets),
                    attachments=[dict(filename=a.filename,size=a.size) for a in snapshot.assets if a.kind=='attachment'],
                    total_bytes=sum(a.size or 0 for a in snapshot.assets),
                    sizes=[a.size for a in snapshot.assets],complete=snapshot.complete,
                    warnings=snapshot.warnings),ensure_ascii=False,indent=2),flush=True)
            elif args.compare_first:
                snapshot = await provider.catalogue()
                asset = next(a for a in snapshot.assets if a.kind=='video')
                root = args.output.resolve()/f'miniapp_edify_{provider.account_id}_{provider.bot_id}'
                with sqlite3.connect('file:'+quote(str(root/'catalogue.sqlite3'))+'?mode=ro',uri=True) as db:
                    db.row_factory=sqlite3.Row
                    row = db.execute('SELECT sha256 FROM assets WHERE id=? AND status="done"',(asset.id,)).fetchone()
                    if not row:
                        raise SystemExit('Download and verify the archive first.')
                    digest, received = hashlib.sha256(), 0
                    while received < asset.size:
                        url = await provider.resolve(asset)
                        start = received
                        headers = {'Origin':APP.rstrip('/'),'Referer':APP,'Accept-Encoding':'identity',
                                   'Range':f'bytes={start}-{min(start+8*1024*1024,asset.size)-1}'}
                        async with http.stream('GET',url,headers=headers) as response:
                            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('content-range',''))
                            if response.status_code != 206 or not match:
                                raise SystemExit(f'Independent range read failed: HTTP {response.status_code}')
                            remote_start,end,total=map(int,match.groups())
                            if remote_start != start or total != asset.size:
                                raise SystemExit('Independent range mismatch')
                            async for chunk in response.aiter_raw(1024*1024):
                                digest.update(chunk)
                                received += len(chunk)
                            if received != end+1 or received <= start:
                                raise SystemExit('Independent read was incomplete')
                        print('Independent check MiB:',received//(1024*1024),flush=True)
                    print(json.dumps(dict(independent_bytes=received,independent_sha256=digest.hexdigest())),flush=True)
                    if received != asset.size or digest.hexdigest() != row['sha256']:
                        raise SystemExit('Independent SHA-256 comparison failed.')
                    print(json.dumps(dict(independent_sha256_matches=True,bytes=received)),flush=True)
            else:
                try:
                    report = await archive(provider,args.output,emit)
                    print(json.dumps(report,ensure_ascii=False,default=str,indent=2),flush=True)
                except asyncio.CancelledError:
                    if not args.stop_after_mib:
                        raise
                    print('Stopped deliberately; the next process must resume the .part.',flush=True)
            provider.token = None
    finally:
        await telegram.disconnect()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    account_options=parser.add_mutually_exclusive_group()
    account_options.add_argument('--account',type=int,help='1-based Desktop account index; defaults to the active account')
    account_options.add_argument('--account-id',type=int,help='Select the archive owner regardless of the active Desktop account')
    parser.add_argument('--inspect',action='store_true')
    parser.add_argument('--stop-after-mib',type=int)
    parser.add_argument('--compare-first',action='store_true')
    asyncio.run(main(parser.parse_args()))

"""Catalogue refresh followed by sequential media transfer and offline export."""
import asyncio
import time

import httpx

from . import MiniAppError
from .download import download
from .edify import Edify, tls_context
from .render import render
from .store import Store
from .history import History, default_history_path


async def archive(provider, output, emit=lambda *_: None, history_path=None):
    if provider.account_id is None or provider.bot_id is None:
        await provider.login()
    store = Store(output, provider.source, provider.account_id, provider.bot_id)
    run = store.begin()
    started = time.monotonic()
    transferred = 0
    last_emit = 0
    active_bytes = 0
    baseline = None
    phase = 'catalogue'
    downloaded = 0
    history = None
    def summary(force=False):
        nonlocal last_emit
        timestamp = time.monotonic()
        if not force and timestamp - last_emit < .25:
            return
        last_emit = timestamp
        rows = store.rows()
        current = [a for a in rows if a['current']]
        done = [a for a in current if a['status'] == 'done']
        elapsed = timestamp - started
        speed = transferred / elapsed if elapsed else 0
        remaining = max(0, sum(a['size'] or 0 for a in current) - sum(a['size'] or 0 for a in done) - active_bytes)
        nodes = store.db.execute('SELECT kind,state,current FROM nodes').fetchall()
        emit('mini_summary', dict(phase=phase, files_done=len(done),files_total=len(current),
            files_errors=sum(a['status'] in ('error','unavailable','unsupported') for a in current),
            lessons=sum(n['kind']=='lesson' and n['current'] for n in nodes),
            pending_modules=sum(n['kind']=='module' and n['state']=='pending' and n['current'] for n in nodes),
            done_bytes=sum(a['size'] or 0 for a in done),total_bytes=sum(a['size'] or 0 for a in current),
            active_bytes=active_bytes,speed_bps=speed,
            eta_seconds=remaining/speed if speed and phase == 'media' else None,
            unknown_sizes=any(a['size'] is None for a in current),
            complete=store.meta('catalogue_complete') == '1',downloaded=downloaded))
    def progress(kind, value):
        nonlocal transferred, active_bytes, baseline
        if kind == 'progress':
            _, received, _ = value
            if baseline is not None:
                transferred += max(0, received - baseline)
            baseline = received
            active_bytes = received
            summary()
        emit(kind, value)
    try:
        emit('archive', str(store.root))
        summary(True)
        catalogue = await provider.catalogue()
        if history_path is not None or getattr(provider, 'history_enabled', False):
            history = History(history_path or default_history_path(), provider.source, provider.account_id, provider.bot_id)
            labels = {'new_lesson':'Впервые найден урок', 'new_attachment':'Новое вложение в известном уроке',
                      'source_changed':'Изменился файл в известном уроке'}
            for title, change in history.observe(catalogue):
                emit('log', labels[change] + ': ' + title)
        store.sync(catalogue)
        render(store)
        emit('mini_catalogue', [dict(id=n.id,parent=n.parent,kind=n.kind,title=n.title,state=n.state)
                                for n in catalogue.nodes])
        for warning in catalogue.warnings:
            emit('log', warning)
        phase = 'media'
        summary(True)
        # Only freshly confirmed references are downloaded. Earlier material is
        # retained, even after entitlement changes or an incomplete API response.
        for asset in catalogue.assets:
            active_bytes, baseline = 0, None
            if await download(provider, store, asset, progress):
                downloaded += 1
            if history is not None:
                history.completed(store.db.execute('SELECT * FROM assets WHERE id=?',(asset.id,)).fetchone())
            active_bytes = 0
            summary(True)
        report = render(store)
        remaining = sum(report['counts'][s] for s in ('pending','error','unavailable','unsupported'))
        complete = catalogue.complete and remaining == 0
        phase = 'finished' if complete else 'incomplete'
        store.finish(run, phase)
        summary(True)
        emit('status', ('Все доступные сейчас материалы скачаны.' if complete else
                       'Архив не завершён. Подробности — в каталоге и журнале.') +
             (f' Разделов без доступных материалов: {report["pending_modules"]}.' if report['pending_modules'] else ''))
        return dict(root=store.root,downloaded=downloaded,**report)
    except asyncio.CancelledError:
        phase = 'cancelled'
        store.finish(run, phase)
        render(store)
        summary(True)
        raise
    except BaseException:
        phase = 'incomplete'
        store.meta('catalogue_complete', '0')
        store.finish(run, phase, 'Не удалось завершить обновление каталога или файлов.')
        render(store)
        summary(True)
        raise
    finally:
        store.close()
        if history is not None:
            history.close()


async def export_edify(telegram, output, emit=lambda *_: None, proxy=None):
    # Keep API auth in per-request headers, never in client defaults. In
    # particular, a media/CDN request must not inherit the API's Bearer token.
    async with httpx.AsyncClient(verify=tls_context(), proxy=proxy, follow_redirects=False,
                                timeout=httpx.Timeout(45, connect=20), trust_env=False) as http:
        provider = Edify(telegram, http, emit)
        try:
            return await archive(provider, output, emit)
        except httpx.HTTPError as exc:
            raise MiniAppError('Ошибка подключения к Edify: ' + type(exc).__name__) from None
        finally:
            provider.token = None

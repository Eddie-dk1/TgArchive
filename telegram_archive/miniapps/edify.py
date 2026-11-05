"""Edify's read-only student API. Tokens and signed URLs live only in memory."""
import asyncio
import ipaddress
import re
import ssl
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

import httpx
from telethon.tl.functions.messages import RequestMainWebViewRequest

from . import AccessDenied, Asset, Catalogue, MiniAppError, Node, identity

API = 'https://api.edify.su'
APP = 'https://app.edify.su/'
BOT = 'edify_platform_bot'
IMAGE = re.compile(r'!\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)')


def tls_context():
    # python.org Python may have no installed CA links. Use macOS's CA bundle,
    # preserving certificate/hostname verification; certifi is HTTPX's fallback.
    import certifi
    return ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').exists()
                                      else certifi.where())


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise MiniAppError('Сервер вернул небезопасную ссылку на материал.')
    host = parsed.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')) or parsed.port not in (None, 443):
        raise MiniAppError('Недопустимый адрес материала.')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise MiniAppError('Недопустимый адрес материала.')
    except ValueError:
        pass
    return url


def file_reference(url):
    """Persist an Edify storage key, never an expiring URL's query credentials."""
    parsed = urlsplit(urljoin(APP, url))
    if parsed.hostname in ('app.edify.su', 'api.edify.su'):
        key = parse_qs(parsed.query).get('key', [''])[0]
        if key:
            if parsed.path in ('/public/course-cover', '/public/lesson-media'):
                return {'type':'public_key','path':parsed.path,'key':key}
            return {'type': 'key', 'key': key}
        if parsed.path.startswith('/files/public/'):
            return {'type': 'key', 'key': unquote(parsed.path[len('/files/public/'):])}
    # Unsigned public HTTPS images may be hosted separately. Unknown query tokens
    # are not persisted; these references become explicit unsupported items.
    if parsed.scheme == 'https' and not parsed.query and not parsed.fragment:
        return {'type': 'public', 'url': validate_url(parsed.geturl())}
    return None


def clean_markdown(text):
    # Keep lesson text, but redact potentially signed URLs from local metadata.
    def replace(match):
        value = match.group(0)
        trimmed = value.rstrip(')>.,')
        suffix = value[len(trimmed):]
        parsed = urlsplit(trimmed)
        if parsed.query or parsed.fragment:
            ref = file_reference(trimmed)
            return (('edify-file:' + quote(ref['key'], safe='')) if ref and ref['type'] in ('key','public_key')
                    else '[ссылка скрыта]') + suffix
        return value
    return re.sub(r'https?://[^\s<>]+', replace, text or '')


def items(data, key='items'):
    result = data.get(key)
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise MiniAppError('Формат каталога Edify изменился. Сохранённый архив оставлен на месте.')
    return result


class Edify:
    history_enabled = True
    source = 'edify'

    def __init__(self, telegram, http, emit=lambda *_: None):
        self.telegram, self.http, self.emit = telegram, http, emit
        self.token = None
        self.account_id = None
        self.bot_id = None

    async def login(self):
        me = await self.telegram.get_me()
        bot = await self.telegram.get_input_entity(BOT)
        web = await self.telegram(RequestMainWebViewRequest(peer=bot, bot=bot, platform='tdesktop'))
        parsed = urlsplit(web.url)
        if parsed.scheme != 'https' or parsed.hostname != 'app.edify.su':
            raise MiniAppError('Адрес мини-приложения Edify изменился. Требуется проверка интеграции.')
        init_data = parse_qs(parsed.fragment).get('tgWebAppData', [''])[0]
        if not init_data:
            raise MiniAppError('Telegram не вернул данные входа в мини-приложение.')
        response = await self._request('POST', '/auth/telegram', json={'initData': init_data}, auth=False)
        token = response.get('accessToken')
        if not isinstance(token, str) or not token:
            raise MiniAppError('Edify не вернул авторизацию. Откройте мини-приложение в Telegram и повторите.')
        self.token, self.account_id, self.bot_id = token, me.id, bot.user_id

    async def _request(self, method, path, *, auth=True, **kwargs):
        for attempt in range(3):
            try:
                headers = {'Accept': 'application/json', 'Origin': APP.rstrip('/'), 'Referer': APP}
                if auth:
                    headers['Authorization'] = 'Bearer ' + self.token
                response = await self.http.request(method, API + path, headers=headers, **kwargs)
                if response.status_code == 401 and auth and attempt == 0:
                    await self.login()
                    continue
                if response.status_code in (401, 403, 404):
                    raise AccessDenied(f'Edify: материал недоступен аккаунту (HTTP {response.status_code}).')
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        await asyncio.sleep(min(10, 2 ** attempt))
                        continue
                if response.status_code != 200:
                    raise MiniAppError(f'Edify: ошибка сервера HTTP {response.status_code}.')
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 2:
                    raise MiniAppError('Не удалось прочитать Edify: проверьте сеть и повторите. ' + type(exc).__name__) from None
                await asyncio.sleep(2 ** attempt)
        raise MiniAppError('Edify временно недоступен. Повторите позже.')

    async def catalogue(self):
        result = Catalogue()
        seen = set()
        def warn(message):
            result.complete = False
            result.warnings.append(message)
        async def lesson(raw, parent, unlocked=None):
            lid = str(raw['id'])
            if lid in seen:
                return
            seen.add(lid)
            node = Node('lesson:' + lid, parent, 'lesson', raw['title'], int(raw.get('order', 0)))
            result.nodes.append(node)
            if unlocked is not None and lid not in unlocked:
                node.state = 'unavailable'
                return
            self.emit('status', 'Читаю урок: ' + node.title)
            try:
                full = (await self._request('GET', '/lessons/' + quote(lid, safe='')))['lesson']
                node.text = clean_markdown(full.get('contentMarkdown'))
                result.assets.extend(lesson_assets(node.id, full))
                materials = items(await self._request('GET', '/lessons/' + quote(lid, safe='') + '/materials'))
                for material in materials:
                    result.assets.append(key_asset(node.id, 'material:' + str(material['id']),
                        material['fileKey'], material['filename'], 'attachment', material.get('sizeBytes'),
                        material.get('createdAt')))
            except AccessDenied:
                node.state = 'unavailable'
                warn('Не удалось прочитать доступный урок: ' + node.title)
            except (MiniAppError, KeyError, TypeError, ValueError):
                node.state = 'incomplete'
                warn('Не удалось полностью прочитать урок: ' + node.title)
        courses = items(await self._request('GET', '/me/courses'))
        if not courses:
            warn('У этого аккаунта нет доступных курсов Edify. Проверьте выбранный аккаунт Telegram.')
            return result
        for entry in courses:
            course = entry['course']
            cid = str(course['id'])
            cnode = Node('course:' + cid, '', 'course', course['title'], text=clean_markdown(course.get('description')))
            result.nodes.append(cnode)
            result.assets.extend(lesson_assets(cnode.id, dict(id=cid,
                contentMarkdown=course.get('description'), updatedAt=course.get('updatedAt'))))
            if course.get('coverUrl'):
                covers = lesson_assets(cnode.id, dict(id=cid, updatedAt=course.get('updatedAt'),
                    contentMarkdown='![Обложка](<' + course['coverUrl'] + '>)'))
                for cover in covers:
                    cover.label = 'cover'
                result.assets.extend(covers)
            self.emit('status', 'Читаю курс: ' + cnode.title)
            try:
                details = await self._request('GET', '/courses/' + quote(cid, safe=''))
                modules = items(await self._request('GET', f'/courses/{quote(cid, safe="")}/modules'))
                for module in modules:
                    mid = str(module['id'])
                    mnode = Node('module:' + mid, cnode.id, 'module', module['title'], int(module.get('position', 0)))
                    result.nodes.append(mnode)
                    try:
                        listing = await self._request('GET', f'/courses/{quote(cid, safe="")}/modules/{quote(mid, safe="")}/lessons')
                        rows = items(listing)
                        if not rows:
                            mnode.state = 'pending'
                        unlocked = listing.get('unlockedLessonIds')
                        for raw in rows:
                            await lesson(raw, mnode.id, unlocked)
                        # Preserve unexpected catalogue types as visible limitations;
                        # never silently announce a complete export of them.
                        for key in ('podcasts', 'attestations'):
                            if listing.get(key):
                                warn(f'{mnode.title}: тип материалов «{key}» пока не поддерживается.')
                    except (MiniAppError, KeyError, TypeError, ValueError):
                        mnode.state = 'incomplete'
                        warn('Не удалось прочитать раздел: ' + mnode.title)
                for raw in items(details, 'lessons'):
                    await lesson(raw, cnode.id, details.get('unlockedLessonIds'))
                podcasts = await self._request('GET', f'/courses/{quote(cid, safe="")}/podcasts/course-level')
                if items(podcasts):
                    warn(cnode.title + ': подкасты пока не поддерживаются.')
            except (MiniAppError, KeyError, TypeError, ValueError):
                cnode.state = 'incomplete'
                warn('Не удалось полностью прочитать курс: ' + cnode.title)
        return result

    async def resolve(self, asset):
        locator = asset.locator
        if locator['type'] == 'video':
            latest = (await self._request('GET', '/lessons/' + quote(locator['lesson'], safe='')))['lesson']
            candidates = lesson_assets(asset.node, latest)
            if not any(a.id == asset.id for a in candidates):
                raise MiniAppError('Материал изменился после чтения каталога. Обновите каталог перед докачкой.')
            data = await self._request('GET', f'/lessons/{quote(locator["lesson"], safe="")}/videos/{locator["index"]}/stream-url')
            url = data['url']
        elif locator['type'] == 'key':
            data = await self._request('GET', '/files/signed', params={'key': locator['key']})
            url = data['url']
        elif locator['type'] == 'public':
            url = locator['url']
        elif locator['type'] == 'public_key' and locator['path'] in ('/public/course-cover','/public/lesson-media'):
            url = API + locator['path'] + '?' + urlencode({'key':locator['key']})
        else:
            raise MiniAppError(asset.problem or 'Этот формат материала пока не поддерживается.')
        return validate_url(urljoin(API, url))


def key_asset(node, label, key, filename, kind, size=None, revision=None):
    return Asset(node, label, filename, kind, {'type': 'key', 'key': key},
                 identity([key, size, revision]), size)


def lesson_assets(node_id, lesson):
    result = []
    # An explicitly empty `videos` array takes precedence over the legacy field.
    videos = lesson.get('videos') if isinstance(lesson.get('videos'), list) else [lesson.get('video')]
    for index, video in enumerate(videos):
        if not video or video.get('kind') == 'none':
            continue
        kind = video.get('kind')
        size = video.get('sizeBytes') if kind == 'telegram' else video.get('size')
        if kind == 'telegram':
            fingerprint = identity([kind, video['fileId'], video.get('chatId'), video.get('messageId'), size])
            filename, locator, problem = f'video_{index + 1}.mp4', {'type': 'video', 'lesson': str(lesson['id']), 'index': index}, ''
        elif kind == 'upload':
            fingerprint = identity([kind, video['key'], size])
            filename, locator, problem = video['originalFilename'], {'type': 'key', 'key': video['key']}, ''
        else:
            fingerprint = identity(video)
            filename, locator = f'video_{index + 1}.mp4', {'type': 'unsupported'}
            problem = f'Внешнее видео «{kind}» пока не поддерживается.'
        result.append(Asset(node_id, f'video:{index}', filename, 'video', locator, fingerprint, size,
                            kind == 'telegram' and isinstance(size, int) and size > 0, problem))
    for index, image in enumerate((lesson.get('slider') or {}).get('images', [])):
        key = image['key']
        asset = key_asset(node_id, f'photo:{index}', key, Path(urlsplit(key).path).name or f'photo_{index+1}.jpg',
                          'photo', revision=lesson.get('updatedAt'))
        asset.locator = {'type':'public_key','path':'/public/lesson-media','key':key}
        result.append(asset)
    for index, match in enumerate(IMAGE.finditer(lesson.get('contentMarkdown') or '')):
        url = match.group(1).strip('<>')
        ref = file_reference(url)
        name = Path(urlsplit(url).path).name or f'image_{index + 1}.jpg'
        if ref and ref['type'] == 'key':
            asset = key_asset(node_id, f'inline:{index}', ref['key'], Path(ref['key']).name, 'photo', revision=lesson.get('updatedAt'))
        elif ref:
            asset = Asset(node_id, f'inline:{index}', Path(ref.get('key',name)).name, 'photo', ref,
                          identity([ref, lesson.get('updatedAt')]))
        else:
            asset = Asset(node_id, f'inline:{index}', f'image_{index+1}.jpg', 'photo', {'type':'unsupported'},
                          identity(url), problem='Не удалось получить постоянный ключ фотографии.')
        result.append(asset)
    presentation = lesson.get('presentation') or {}
    for field, ext in (('pdfKey', '.pdf'), ('pptxKey', '.pptx')):
        if presentation.get(field):
            name = Path(presentation.get('originalFilename') or 'presentation').stem + ext
            result.append(key_asset(node_id, 'presentation:' + field, presentation[field], name,
                                    'attachment', revision=lesson.get('updatedAt')))
    return result

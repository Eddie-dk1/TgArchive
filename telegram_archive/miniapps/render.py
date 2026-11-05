"""Safe, self-contained HTML with local attachments and escaped lesson text."""
import html
import json
from urllib.parse import quote


def render(store):
    esc = html.escape
    nodes = store.db.execute('SELECT * FROM nodes ORDER BY position,rowid').fetchall()
    assets = store.rows()
    children = {}
    for node in nodes:
        children.setdefault(node['parent'], []).append(node)
        # Every lesson keeps a human-readable source text alongside its files.
        store.write_text(node['path'] + '/lesson.md', '# ' + node['title'] + '\n\n' + node['text'] + '\n')
    by_node = {}
    for asset in assets:
        if asset['current'] or asset['status'] == 'done':
            by_node.setdefault(asset['node'], []).append(asset)
    states = {'pending':'Материалы пока недоступны — проверьте позже',
              'unavailable':'Материал пока недоступен аккаунту',
              'incomplete':'Раздел прочитан не полностью — обновите каталог'}
    def block(node):
        label = '<span class="badge">Ранее сохранённое</span>' if not node['current'] else ''
        result = f'<section><h2>{esc(node["title"])} {label}</h2>'
        if node['state'] in states:
            result += '<p class="notice">' + states[node['state']] + '</p>'
        result += '<div class="text">' + esc(node['text']) + '</div>'
        for asset in by_node.get(node['id'], []):
            url, name = quote(asset['path'], safe='/'), esc(asset['filename'])
            if not asset['current']:
                name += ' <span class="badge">Ранее сохранённая версия</span>'
            status = asset['status']
            if status == 'done':
                if asset['kind'] == 'video':
                    result += f'<video controls preload="metadata" src="{url}"></video>'
                elif asset['kind'] == 'photo':
                    result += f'<a href="{url}"><img loading="lazy" src="{url}" alt="{esc(asset["filename"])}"></a>'
                result += f'<p><a href="{url}" download>{name}</a></p>'
            else:
                result += '<p class="notice">' + name + ': ' + esc(asset['error'] or 'Ожидает скачивания') + '</p>'
        for child in children.get(node['id'], []):
            result += block(child)
        return result + '</section>'
    warnings = json.loads(store.meta('catalogue_warnings') or '[]')
    notice = ''.join('<p class="notice">' + esc(w) + '</p>' for w in warnings)
    counts = {status:sum(a['status'] == status and a['current'] for a in assets)
              for status in ('done','pending','error','unavailable','unsupported')}
    complete = store.meta('catalogue_complete') == '1' and not sum(counts[k] for k in counts if k != 'done')
    state = 'Все доступные сейчас материалы скачаны' if complete else 'Архив не завершён — обновите и докачайте'
    body = ''.join(block(n) for n in children.get('', []))
    page = '''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; media-src 'self' file:; style-src 'unsafe-inline'">
<title>Edify · Telegram Archive</title><style>
:root{color-scheme:dark}body{margin:0;background:#101c2b;color:#e1ebf5;font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif}
main{max-width:1000px;margin:40px auto;padding:0 24px}header{border-bottom:1px solid #304258;padding-bottom:20px}
h1{font-size:30px}h2{font-size:23px;font-weight:600}section{margin:28px 0;padding:0 0 0 18px;border-left:1px solid #304258}
section section h2{font-size:20px}.text{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#73b6e8}
video,img{display:block;max-width:100%;max-height:70vh;margin:16px 0}video{width:100%;background:#000}
.notice{color:#edbe74}.badge{font-size:12px;font-weight:400;color:#9fb3ca}footer{color:#9fb3ca;margin-top:32px}
</style><main><header><h1>Telegram Archive · Edify</h1>'''
    page += f'<p>{state}</p><p>Файлов сохранено: {counts["done"]} · Проверено: {esc(store.meta("catalogue_updated") or "—")}</p>'
    page += notice + '</header>' + body + '<footer>Разделы, которые откроются позже, появятся после «Обновить и докачать». Прогресс обучения не изменяется.</footer></main></html>'
    store.write_text('index.html', page)
    problems = [dict(filename=a['filename'],status=a['status'],attempts=a['attempts'],error=a['error'])
                for a in assets if a['current'] and a['status'] != 'done']
    report = {'catalogue_complete': store.meta('catalogue_complete') == '1', 'warnings':warnings,
              'counts':counts,'problems':problems,
              'pending_modules':sum(n['kind']=='module' and n['state']=='pending' and n['current'] for n in nodes)}
    store.write_text('report.json', json.dumps(report, ensure_ascii=False, indent=2))
    return report

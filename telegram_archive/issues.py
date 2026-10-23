"""Read-only, paginated inspection of an existing archive, including older schemas."""
import json
import re
import sqlite3
from pathlib import Path

PAGE_SIZE = 100
RETRY_CONDITION = "category IS NOT NULL AND status!='done' AND (status IN ('error','unavailable') OR error IS NOT NULL)"


def read_issues(folder, offset=0):
    root = Path(folder).expanduser().resolve()
    match = re.fullmatch(r'channel_(-?\d+)', root.name)
    if not match or not (root / 'archive.sqlite3').is_file():
        raise ValueError('Выберите папку channel_<ID>, в которой находится archive.sqlite3.')
    connection = sqlite3.connect((root / 'archive.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        owner = connection.execute("SELECT value FROM metadata WHERE key='account_id'").fetchone()
        if owner is None:
            raise ValueError('В архиве не указан аккаунт. Сначала откройте его обычной выгрузкой.')
        owner = json.loads(owner[0])
        condition = "category IS NOT NULL AND status NOT IN ('done','none')"
        total = connection.execute(f'SELECT count(*) FROM messages WHERE {condition}').fetchone()[0]
        retryable = connection.execute(f'SELECT count(*) FROM messages WHERE {RETRY_CONDITION}').fetchone()[0]
        offset = min(max(0, offset), max(0, (total-1)//PAGE_SIZE * PAGE_SIZE))
        rows = [dict(row) for row in connection.execute(
            f'SELECT id,date,category,status,error,attempts,link FROM messages WHERE {condition} ORDER BY id LIMIT ? OFFSET ?',
            (PAGE_SIZE, offset))]
        title = root.name
        info_path = root / 'channel.json'
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding='utf-8'))
                title = info.get('title') or title
            except (OSError, ValueError):
                pass
        return dict(root=root, id=int(match[1]), account_id=owner, title=title,
                    rows=rows, total=total, retryable=retryable, offset=offset)
    finally:
        connection.close()

"""Shared serialization and media helpers (classification adapted from zsv555, MIT)."""
import base64
import json
import re
from datetime import datetime

from telethon.tl.types import (DocumentAttributeAnimated, DocumentAttributeAudio,
                               DocumentAttributeSticker, DocumentAttributeVideo)

BLOCK = 512 * 1024
PAGE_SIZE = 500


def now():
    return datetime.now().astimezone().isoformat()


def safe_name(value, fallback='file'):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', value or fallback)
    return (value.strip(' .') or fallback).encode('utf-8')[:160].decode('utf-8', errors='ignore')


def classify_message(message):
    if message.photo:
        return 'photos'
    if not message.document:
        return None
    attrs = message.document.attributes
    if any(isinstance(a, (DocumentAttributeSticker, DocumentAttributeAnimated)) for a in attrs):
        return 'stickers_gifs'
    if any(isinstance(a, DocumentAttributeVideo) for a in attrs):
        return 'videos'
    if any(isinstance(a, DocumentAttributeAudio) for a in attrs):
        return 'audio'
    return 'documents'


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {'base64': base64.b64encode(value).decode('ascii')}
    raise TypeError(type(value).__name__)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=json_default)


def atomic_text(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(value, encoding='utf-8')
    tmp.replace(path)


def expected_size(raw):
    media = raw.get('media') or {}
    if media.get('document'):
        return media['document'].get('size')
    sizes = []
    for size in (media.get('photo') or {}).get('sizes', []):
        if size.get('size') is not None:
            sizes.append(size['size'])
        sizes.extend(size.get('sizes', []))
    return max(sizes) if sizes else None


def format_bytes(value):
    value = max(0, value or 0)
    for unit in ('Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'):
        if value < 1024 or unit == 'ТиБ':
            return f'{value:.1f} {unit}'
        value /= 1024


def format_duration(seconds):
    if seconds is None:
        return 'рассчитывается'
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours} ч {minutes:02} мин' if hours else f'{minutes} мин {seconds:02} с'

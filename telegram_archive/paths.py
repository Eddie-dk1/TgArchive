"""Writable profile locations for source runs and read-only macOS app bundles."""
import os
import sys
from pathlib import Path


def private_directory(base):
    override = os.environ.get('TG_ARCHIVE_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        return Path.home() / 'Library' / 'Application Support' / 'Telegram Archive'
    return Path(base) / '.private'

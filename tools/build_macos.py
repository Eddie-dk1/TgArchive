"""Build and zip a standalone, ad-hoc signed macOS app; never include local profiles."""
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != 'darwin':
        raise SystemExit('Build the macOS app on a Mac.')
    import tkinter
    from build_icon import build_icon
    build_icon()
    environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT/'build/pyinstaller-cache'))
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean',
                    '--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build'),
                    str(ROOT/'packaging/TelegramArchive.spec')], cwd=ROOT, env=environment, check=True)
    app = ROOT/'dist/Telegram Archive.app'
    subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
    forbidden = [p for p in app.rglob('*') if p.name in ('.private','tdata','settings.json','desktop_connection.json')
                 or '.session' in p.name or p.suffix in ('.sqlite3','.jsonl')]
    if forbidden:
        raise SystemExit('Unexpected private/runtime data in app bundle; refusing to package it.')
    name=f'Telegram-Archive-macOS-{platform.machine()}.zip'
    target=ROOT/'dist'/name
    subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(app),str(target)],check=True)
    with target.open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    target.with_suffix('.zip.sha256').write_text(f'{digest}  {name}\n')
    print(f'Built: {target}',flush=True)


if __name__ == '__main__':
    main()

# A directory bundle keeps Tcl/Tk resources in place and avoids per-launch extraction.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
namespace = {}
exec((root / 'telegram_archive/__init__.py').read_text(), namespace)
datas = [(str(root / 'LICENSE'), '.')]
binaries = []
hiddenimports = ['python_socks.async_.asyncio']
for package in ('opentele2',):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports

a = Analysis([str(root / 'packaging/macos_entry.py')], pathex=[str(root)],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=['pytest', 'IPython', 'notebook'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Telegram Archive',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, argv_emulation=False, target_arch=None,
          codesign_identity=None, entitlements_file=None)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Telegram Archive')
app = BUNDLE(collection, name='Telegram Archive.app',
             icon=str(root / 'assets/archive.icns'),
             bundle_identifier='io.github.eddie-dk1.tgarchive',
             info_plist={'CFBundleShortVersionString': namespace['__version__'],
                         'CFBundleVersion': namespace['__version__'],
                         'NSHighResolutionCapable': True,
                         'NSHumanReadableCopyright': 'MIT License; see bundled LICENSE'})

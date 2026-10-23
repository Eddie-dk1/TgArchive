"""Exercise the built executable in a temporary profile without Telegram access."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    app = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'dist/Telegram Archive.app'
    executable = app/'Contents/MacOS/Telegram Archive'
    with tempfile.TemporaryDirectory(prefix='tgarchive-smoke-') as temporary:
        result = Path(temporary)/'result.json'
        environment = dict(os.environ, TG_ARCHIVE_DATA_DIR=str(Path(temporary)/'profile'))
        try:
            process = subprocess.run([str(executable),'--smoke-test','--smoke-result',str(result)],
                                     env=environment, timeout=45)
        except subprocess.TimeoutExpired:
            raise SystemExit('Bundle smoke timed out: ' + (result.read_text() if result.exists() else 'entry point did not start'))
        data = json.loads(result.read_text())
        if data.get('ok') is not True or process.returncode:
            raise SystemExit('Bundle smoke check failed: '+json.dumps(data))
        print(json.dumps(data, indent=2))


if __name__ == '__main__':
    main()

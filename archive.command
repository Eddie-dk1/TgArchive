#!/bin/zsh
set -e
cd -- "${0:A:h}"
umask 077
if [[ ! -x .venv/bin/python ]]; then
    if ! command -v python3 >/dev/null; then
        print 'Нужен Python 3.11 или новее с python.org. Установите и запустите снова.'
        read -k 1
        exit 1
    fi
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi
if ! .venv/bin/python -c 'import qrcode, opentele2, httpx, socksio' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m telegram_archive

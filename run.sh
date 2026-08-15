#!/bin/zsh
set -e
cd "${0:A:h}"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765

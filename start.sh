#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt
python build_frontend.py
# Python loads the optional .env and resolves the same data directory as the server.
python - <<'PY'
import os, subprocess, sys
import runtime_settings
if os.getenv('ATLAS_REQUIRE_LOGIN','1') == '1' and not (runtime_settings.DATA/'auth.json').exists():
    subprocess.run([sys.executable, 'setup_password.py'], check=True)
print('Email Dashboard: http://localhost:' + os.getenv('ATLAS_PORT','8765'))
print('自动刷新默认暂停。打开「邮箱与 API 设置」连接自己的账户。')
PY
exec python server.py

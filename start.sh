#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install -q -r requirements.txt
export ATLAS_REQUIRE_LOGIN="${ATLAS_REQUIRE_LOGIN:-0}"
if [ "$ATLAS_REQUIRE_LOGIN" = "1" ] && [ ! -f data/auth.json ]; then
  python setup_password.py
fi
if [ "$ATLAS_REQUIRE_LOGIN" = "1" ]; then
  echo "Dashboard 已启用密码登录。"
else
  echo "Dashboard 免登录；局域网内可访问此地址的设备可以查看和编辑。"
fi
echo "访问地址: http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo '<Mac-IP>'):8765"
echo "停止服务：按 Ctrl+C；不要在路由器上做端口转发。"
ATLAS_BIND="${ATLAS_BIND:-0.0.0.0}" ATLAS_PORT="${ATLAS_PORT:-8765}" python server.py

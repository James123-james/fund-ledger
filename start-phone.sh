#!/usr/bin/env bash
# 手机本地启动：默认只监听 127.0.0.1，账本数据留在这台手机上。
#
#   bash start-phone.sh                 # http://127.0.0.1:8787
#   PORT=9000 bash start-phone.sh       # 换端口
#   DATA=demo bash start-phone.sh       # 用演示账本
#   HOST=0.0.0.0 bash start-phone.sh    # 同一 WiFi 的电脑/平板也能看（谨慎）
#
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
DATA="${DATA:-$PWD/data}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 $PYTHON。请先安装 Python 3.10 或更高版本。"
  exit 1
fi

echo "基金台账 · 手机本地版"
echo "  打开：http://$HOST:$PORT"
echo "  数据：$DATA/fund.db"
echo "  停止：Ctrl+C"
echo ""

exec "$PYTHON" server.py --host "$HOST" --port "$PORT" --data "$DATA" --no-browser

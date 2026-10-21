#!/bin/bash
# Mở Văn phòng Claude: bật máy chủ nếu chưa chạy rồi mở trang trong trình duyệt.
#   bash chay.sh          # bật + mở trang
#   bash chay.sh --tat    # tắt máy chủ
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${VAN_PHONG_PORT:-4747}"
STATE="$HOME/.claude/van-phong"
mkdir -p "$STATE"

dang_chay() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }

if [ "${1:-}" = "--tat" ]; then
  PID=$(dang_chay)
  [ -n "$PID" ] && kill "$PID" && echo "Đã tắt (PID $PID)" || echo "Không có máy chủ nào đang chạy"
  exit 0
fi

if [ -z "$(dang_chay)" ]; then
  nohup python3 "$DIR/server.py" --port "$PORT" >>"$STATE/server.out" 2>&1 &
  for _ in $(seq 1 20); do
    [ -n "$(dang_chay)" ] && break
    perl -e 'select(undef,undef,undef,0.3)'
  done
fi
if [ -z "$(dang_chay)" ]; then
  echo "Máy chủ không lên, xem $STATE/server.out" >&2
  exit 1
fi
open "http://127.0.0.1:$PORT/"
echo "Văn phòng Claude: http://127.0.0.1:$PORT/"

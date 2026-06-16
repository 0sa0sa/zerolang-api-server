#!/bin/sh
# Launch the full Todo BE: Zero read backend + external writer sidecar.
#
#   client ──> writer.py (:8080, owns writes + persistence)
#                 │  GET /health,/todos
#                 └─> zero run  (:3000+, reads data/store.json via std.fs.readBytes)
#
# NOTE: `zero` MUST be invoked by full path — std.http.listen re-execs argv[0],
# so a bare PATH-resolved `zero run` fails with "zero listen: No such file or directory".
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
ZBIN="${ZERO_BIN:-$HOME/.zero/bin/zero}"
STORE="${ZT_STORE:-$DIR/data/store.json}"
PORT="${ZT_PORT:-8080}"
cd "$DIR"

mkdir -p "$DIR/data"
[ -f "$STORE" ] || printf '[]' > "$STORE"

ZLOG="$DIR/data/zero.log"
: > "$ZLOG"
"$ZBIN" run > "$ZLOG" 2>&1 &
ZPID=$!
trap 'kill $ZPID 2>/dev/null || true' EXIT INT TERM

# Wait for the Zero listener to announce its (auto-incremented) port.
BACKEND=""
i=0
while [ $i -lt 40 ]; do
  BACKEND=$(grep -oE '127\.0\.0\.1:[0-9]+' "$ZLOG" | head -1 || true)
  [ -n "$BACKEND" ] && break
  if grep -qiE 'BLD|PAR|ERR|error' "$ZLOG"; then echo "zero backend failed:"; cat "$ZLOG"; exit 1; fi
  i=$((i+1)); sleep 0.25
done
[ -n "$BACKEND" ] || { echo "zero backend did not start:"; cat "$ZLOG"; exit 1; }
echo "zero read-backend: http://$BACKEND"

ZT_STORE="$STORE" ZERO_BACKEND="$BACKEND" ZT_PORT="$PORT" exec python3 "$DIR/writer.py"

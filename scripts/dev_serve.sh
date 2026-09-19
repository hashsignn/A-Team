#!/usr/bin/env bash
# Start the API for local checking. In a script so a `pkill -f` pattern never
# appears in the calling shell's own argv (that kills the caller).
set -u
PORT="${1:-8600}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT/.dev-api.log"
for pid in $(pgrep -f "uvicorn" || true); do
  [ "$pid" = "$$" ] && continue
  kill "$pid" 2>/dev/null && echo "stopped $pid"
done
sleep 1
cd "$ROOT" || exit 1
nohup .venv/bin/python -m uvicorn api.main:app --host 0.0.0.0 --port "$PORT" > "$LOG" 2>&1 &
for _ in $(seq 1 60); do
  sleep 1
  if [ "$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://localhost:$PORT/api/health" || true)" = "200" ]; then
    echo "serving on $PORT"; exit 0
  fi
done
echo "did not come up:"; tail -20 "$LOG"; exit 1

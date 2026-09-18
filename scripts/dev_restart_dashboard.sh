#!/usr/bin/env bash
# Restart the dashboard for local checking.
#
# Lives in a file rather than being typed inline because a `pkill -f` whose
# pattern appears in the invoking shell's own command line kills the shell that
# ran it. Running from a script keeps the pattern out of the caller's argv.
set -u

PORT="${1:-8512}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${2:-$ROOT/.dev-dashboard.log}"

for pid in $(pgrep -f "app\.py" || true); do
  [ "$pid" = "$$" ] && continue
  kill "$pid" 2>/dev/null && echo "stopped $pid"
done
sleep 1

cd "$ROOT" || exit 1
nohup .venv/bin/python -m streamlit run dashboard/app.py \
  --server.headless true \
  --server.port "$PORT" \
  --browser.gatherUsageStats false > "$LOG" 2>&1 &

for _ in $(seq 1 40); do
  sleep 1
  code=$(curl -s --max-time 2 -o /dev/null -w "%{http_code}" "http://localhost:$PORT" || true)
  if [ "$code" = "200" ]; then
    echo "serving on $PORT"
    exit 0
  fi
done

echo "did not come up; log tail:"
tail -20 "$LOG"
exit 1

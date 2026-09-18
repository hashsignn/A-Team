#!/usr/bin/env bash
# Push the designated branch, retrying through transient credential/network
# failures with exponential backoff.
set -u

BRANCH="${1:-claude/elegant-clarke-711wkt}"
MAX="${2:-6}"
delay=15

for attempt in $(seq 1 "$MAX"); do
  echo "--- attempt $attempt/$MAX ---"
  if out=$(git push -u origin "$BRANCH" 2>&1); then
    echo "$out" | tail -3
    echo "PUSHED"
    exit 0
  fi
  echo "$out" | tail -2
  if [ "$attempt" -lt "$MAX" ]; then
    echo "retrying in ${delay}s"
    sleep "$delay"
    delay=$((delay * 2))
    [ "$delay" -gt 120 ] && delay=120
  fi
done

echo "STILL FAILING after $MAX attempts"
exit 1

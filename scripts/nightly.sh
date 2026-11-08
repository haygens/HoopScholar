#!/usr/bin/env bash
# Cron wrapper for the nightly update (macOS / Linux).
# Logs to data/logs/, and skips the run if the last one is still going.
#
# Install (runs 5:17 AM local time; games finish by ~1 AM Pacific):
#   crontab -e
#   17 5 * * *  /full/path/to/hoopscholar/scripts/nightly.sh
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p data/logs
LOG="data/logs/nightly-$(date +%F).log"
LOCK="data/.nightly.lock"

# Use the project's virtualenv if there is one
PY="python3"
[ -x .venv/bin/python ] && PY=".venv/bin/python"

exec 9>"$LOCK"
if ! flock -n 9 2>/dev/null; then
  # flock isn't installed on macOS by default; fall back to a pid check
  if [ -f "$LOCK.pid" ] && kill -0 "$(cat "$LOCK.pid")" 2>/dev/null; then
    echo "$(date) previous run still going, skipping" >> "$LOG"; exit 0
  fi
fi
echo $$ > "$LOCK.pid"

{
  echo "=== $(date) ==="
  "$PY" scripts/nightly.py
} >> "$LOG" 2>&1

rm -f "$LOCK.pid"
# keep two weeks of logs
find data/logs -name 'nightly-*.log' -mtime +14 -delete

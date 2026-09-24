#!/usr/bin/env bash
# dogfood.sh — Gate 3 local dogfood harness for numwatch.
#
# Runs demo_service.py (fake job-queue metric + mock Slack webhook) alongside
# `numwatch tick` on a fixed interval, then reports on missed samples,
# duplicate/missing pings, tick.log tracebacks and status.html staleness.
#
# Usage: ./examples/dogfood.sh [DURATION_SECONDS=300] [TICK_INTERVAL_SECONDS=5]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

DURATION="${1:-300}"
TICK_INTERVAL="${2:-5}"
EVERY=10  # matches "every" in examples/dogfood.toml

WORKDIR="examples/.dogfood"
DB="$WORKDIR/numwatch.db"
PAGE="$WORKDIR/status.html"
TICKLOG="$WORKDIR/tick.log"
PINGS="$WORKDIR/pings.jsonl"

mkdir -p "$WORKDIR"
rm -f "$DB" "$PAGE" "$TICKLOG" "$PINGS"

export NUMWATCH_CONFIG="examples/dogfood.toml"
export NUMWATCH_NOTIFY="examples/notify.dogfood.toml"
export NUMWATCH_DB="$DB"
export NUMWATCH_PAGE="$PAGE"
unset NUMWATCH_SLACK_WEBHOOK 2>/dev/null || true

echo "== numwatch dogfood: duration=${DURATION}s tick_interval=${TICK_INTERVAL}s =="

python3 examples/demo_service.py --pings-log "$PINGS" &
SERVICE_PID=$!

cleanup() {
  if kill -0 "$SERVICE_PID" 2>/dev/null; then
    kill "$SERVICE_PID" 2>/dev/null || true
    wait "$SERVICE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "waiting for demo_service to become healthy..."
ready=0
for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "demo_service failed to start within 10s" >&2
  exit 1
fi
echo "demo_service is up."

start_ts=$(date +%s)
last_progress=$start_ts
tick_fail_count=0
max_lag=0

while :; do
  now=$(date +%s)
  elapsed=$(( now - start_ts ))
  if [ "$elapsed" -ge "$DURATION" ]; then
    break
  fi

  set +e
  ./numwatch.py tick >> "$TICKLOG" 2>&1
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    tick_fail_count=$((tick_fail_count + 1))
  fi

  if [ -f "$PAGE" ]; then
    mtime=$(python3 -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$PAGE" 2>/dev/null || echo "$now")
    after=$(date +%s)
    lag=$(( after - mtime ))
    if [ "$lag" -gt "$max_lag" ]; then
      max_lag=$lag
    fi
  fi

  now2=$(date +%s)
  if [ $(( now2 - last_progress )) -ge 30 ]; then
    last_progress=$now2
    elapsed2=$(( now2 - start_ts ))
    echo "-- elapsed=${elapsed2}s --"
    ./numwatch.py status || true
  fi

  sleep "$TICK_INTERVAL"
done

echo ""
echo "== loop done: tick_fail_count=$tick_fail_count max_status_lag=${max_lag}s =="
echo ""

echo "=== ./numwatch.py status ==="
./numwatch.py status || true
echo ""

echo "=== sample counts (watch, count, min_ts, max_ts) ==="
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" "select watch, count(*), min(ts), max(ts) from samples group by watch;"
else
  python3 -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
for row in conn.execute('select watch, count(*), min(ts), max(ts) from samples group by watch'):
    print(row)
" "$DB"
fi
echo ""

python3 - "$DB" "$PINGS" "$TICKLOG" "$DURATION" "$max_lag" "$tick_fail_count" "$EVERY" <<'PY'
import json, os, sqlite3, sys
from collections import Counter

db, pings_path, ticklog, duration_s, max_lag_s, tick_fail_s, every_s = sys.argv[1:8]
duration, max_lag, tick_fail, every = int(duration_s), int(max_lag_s), int(tick_fail_s), int(every_s)
overall_pass = True


def check(label, cond):
    global overall_pass
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        overall_pass = False


print("=== missed sample gaps (> 2x every = %ds) ===" % (2 * every))
conn = sqlite3.connect(db)
for (watch,) in conn.execute("SELECT DISTINCT watch FROM samples"):
    ts_list = [r[0] for r in conn.execute("SELECT ts FROM samples WHERE watch=? ORDER BY ts", (watch,))]
    gaps = sum(1 for a, b in zip(ts_list, ts_list[1:]) if b - a > 2 * every)
    print(f"{watch}: missed_gaps={gaps}")

pings = []
if os.path.exists(pings_path):
    with open(pings_path) as f:
        pings = [json.loads(line) for line in f if line.strip()]

print(f"\n=== pings.jsonl ({len(pings)} lines) ===")
for p in pings:
    print(p["iso"], p["text"])

counts, last_verb, dup_found = Counter(), {}, False
for p in pings:
    parts = p["text"].split()
    if len(parts) < 3:
        continue
    watch = parts[1]
    verb = "source " + parts[3] if parts[2] == "source" else parts[2]
    counts[(watch, verb)] += 1
    if last_verb.get(watch) == verb:
        dup_found = True
        print(f"FAIL duplicate consecutive ping: {watch} {verb}")
    last_verb[watch] = verb

print("\n=== ping counts by (watch, verb) ===")
for k, v in sorted(counts.items()):
    print(k, v)

print("\n=== expected vs actual ===")
if duration >= 300:
    expected = {
        ("demo-queue-depth", "BREACH"): 2,
        ("demo-queue-depth", "RECOVERED"): 2,
        ("demo-health", "source FAILING"): 1,
        ("demo-health", "source RECOVERED"): 1,
    }
    for k, exp in expected.items():
        act = counts.get(k, 0)
        check(f"{k[0]} {k[1]}: expected={exp} actual={act}", act == exp)
    check(
        "demo-health RECOVERED ('<1 for 1' rule) never fires (503 -> error sample, not a value match)",
        counts.get(("demo-health", "RECOVERED"), 0) == 0,
    )
else:
    print(f"DURATION={duration}s < 300s: expectations n/a; actual counts printed above")
check("no duplicate consecutive same-verb pings per watch", not dup_found)

print("\n=== tick.log tail (last 20 lines) ===")
tb_count = 0
if os.path.exists(ticklog):
    content = open(ticklog).read()
    tb_count = content.count("Traceback")
    print("\n".join(content.splitlines()[-20:]))
check("tick.log has 0 Tracebacks", tb_count == 0)

print(f"\ntick failures (nonzero exit during loop): {tick_fail}")
check(f"max status.html lag < 120s (was {max_lag}s)", max_lag < 120)

print()
if overall_pass:
    print("GATE3 DOGFOOD: PASS")
    sys.exit(0)
else:
    print("GATE3 DOGFOOD: FAIL")
    sys.exit(1)
PY

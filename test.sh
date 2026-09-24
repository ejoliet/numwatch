#!/usr/bin/env bash
# test.sh — drive numwatch through a real breach + recovery cycle.
#
# Usage:
#   export NUMWATCH_SLACK_WEBHOOK="https://hooks.slack.com/services/..."   # your shell, not this file
#   ./test.sh
#
# Uses the "random" watch (alert_if = "> 70 for 3") because it's the fastest
# way to see BOTH a breach and a recovery without waiting on real CPU/battery
# state. mac-cpu and mac-battery use the same code path with real numbers.
set -euo pipefail

cd "$(dirname "$0")"

if [ -z "${NUMWATCH_SLACK_WEBHOOK:-}" ]; then
  echo "NUMWATCH_SLACK_WEBHOOK is not set — pings will be skipped silently."
  echo "export it first if you want a real Slack ping during this test."
  echo
fi

if [ ! -f numwatch.db ]; then
  python3 numwatch.py init >/dev/null
fi

echo "== status before =="
python3 numwatch.py status

echo
echo "== running 20 forced samples of 'random' (every=10s, alert_if='> 70 for 3') =="
echo "   watch for '!' in the status line — that's BREACH; it clears on RECOVERED"
for i in $(seq 1 20); do
  val=$(python3 numwatch.py run random)
  printf "  sample %2d: %s\n" "$i" "$val"
  sleep 1
done

echo
echo "== status after =="
python3 numwatch.py status

echo
echo "== rendering status.html =="
python3 numwatch.py page
echo "open status.html in a browser to see the sparkline"

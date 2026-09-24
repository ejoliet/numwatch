# numwatch dogfood harness

Local, secret-free stand-in for the real Jenkins CPU / DuckDB lightcurve
targets referenced in Gate 3 of `RDD.md`. It lets the Gate 3 dogfood loop
(sample -> alert state machine -> Slack ping -> `status.html`) be exercised
end-to-end on a laptop, with no cron, no real webhook, and no secrets.

## What's here

- `demo_service.py` — a single stdlib HTTP server that plays two roles:
  a fake "job queue" metric source (`/metrics`, `/metrics.json`, `/health`)
  that follows a deterministic scripted timeline, and a mock Slack incoming
  webhook (`/hooks/slack`) that appends every received ping to a JSON-lines
  log instead of sending anything over the network.
- `dogfood.toml` — a numwatch config pointing two watches (`demo-queue-depth`,
  `demo-health`) at `demo_service.py`.
- `notify.dogfood.toml` — a `[slack]` alias file pointing `dogfood` at the
  mock webhook's localhost URL. Not a secret (see comment in the file);
  real webhook URLs never belong in a committed file.
- `dogfood.sh` — orchestrates the above: starts `demo_service.py`, runs
  `numwatch tick` on an interval for a fixed duration, then reports on missed
  samples, ping counts, duplicate pings, tick.log tracebacks, and
  `status.html` staleness, ending in `GATE3 DOGFOOD: PASS` or `FAIL`.

## How to run

```sh
./examples/dogfood.sh            # 300s run (matches the scripted timeline below)
./examples/dogfood.sh 300 5      # explicit: 300s duration, 5s tick interval
./examples/dogfood.sh 40 5       # short smoke run; expectation checks report "n/a"
```

Everything lands under `examples/.dogfood/` (wiped at the start of each run):
`numwatch.db`, `status.html`, `tick.log`, `pings.jsonl`.

## Expected timeline (300s run)

| t (s)     | `/metrics`        | `/health` | Expected numwatch event                                    |
|-----------|--------------------|-----------|--------------------------------------------------------------|
| 0–59      | 25 + (t%7)          | 200 "1"   | calm                                                          |
| 60–119    | 95 + (t%3)          | 200 "1"   | `demo-queue-depth` **BREACH** (`> 80 for 3`, fires ~t=63)      |
| 120–199   | 30 + (t%5)          | 200 "1"   | `demo-queue-depth` **RECOVERED** (~t=123)                      |
| 150–189   | 30 + (t%5)          | 503       | `demo-health` source **FAILING** (~t=153), then **RECOVERED** (~t=193) |
| 200–239   | 97                  | 200 "1"   | `demo-queue-depth` second **BREACH** (~t=213)                  |
| 240+      | 28                  | 200 "1"   | `demo-queue-depth` second **RECOVERED** (~t=243)               |

`demo-health`'s own rule (`< 1 for 1`) is expected to *never* fire: a 503
response makes `curl -sf` exit non-zero, which numwatch records as an error
sample (`value = None`); `rule_matches` never matches `None`, so only the
source-error track (FAILING/RECOVERED) can ping for that watch. `GET
/timeline` on the running service prints this same schedule for humans.

## Pointing a real cron at this

```
*/? * * * * cd /path/to/numwatch && \
  NUMWATCH_CONFIG=examples/dogfood.toml \
  NUMWATCH_NOTIFY=examples/notify.dogfood.toml \
  NUMWATCH_DB=examples/.dogfood/numwatch.db \
  NUMWATCH_PAGE=examples/.dogfood/status.html \
  ./numwatch.py tick >> examples/.dogfood/tick.log 2>&1
```

with `demo_service.py` running continuously in the background (or swap in a
real `cmd=` for a real target, and a real `notify.toml` alias with a real
webhook, outside this directory).

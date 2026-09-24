# numwatch

>Watch exactly one number — from a shell command, URL, or SQL query — over time. Sparkline, threshold, Slack ping. The anti-Grafana.

**Problem**: 90% of monitoring needs are "watch this one number and tell me when it's weird." The available answers are either a full observability stack (Prometheus/Grafana, VictoriaMetrics, Splunk) or nothing (cron + curl + if). Host monitors (Beszel, Netdata) watch fixed system metrics only. changedetection.io extracts numbers from URLs but cannot run a shell command or hit a database.

**Solution**: A cron-invoked Python CLI. Config is one TOML file. State is one SQLite file. Output is one static HTML status page (strip-chart aesthetic, see numwatch-preview.html) plus Slack pings on threshold breach and recovery.

**Status**: Gates 1–2 passed (2026-09-23). Gate 3 dogfood next. Full spec, gate table, and agent instructions live in [RDD.md](RDD.md).

## Quickstart

Python 3.11+, no dependencies.

```
./numwatch.py init          # writes numwatch.toml (if missing) + numwatch.db
./numwatch.py run example   # sample one watch now, print the value
./numwatch.py status        # terminal table with unicode sparklines
./numwatch.py page          # regenerate status.html
```

Cron: `* * * * * cd /path/to/numwatch && ./numwatch.py tick >> tick.log 2>&1`

Slack: put webhook URLs in `~/.config/numwatch/notify.toml` (`chmod 600`) under `[slack]`, keyed by the `notify` alias in `numwatch.toml`, or export `NUMWATCH_SLACK_WEBHOOK`. Never commit a webhook.

## Dogfood locally (no secrets)

`examples/` ships a stdlib fake metric service plus a mock Slack receiver with a scripted breach/recovery timeline.

```
./examples/dogfood.sh 300     # 5-minute run, prints GATE3 DOGFOOD: PASS|FAIL
```

See [examples/README.md](examples/README.md) for the expected event timeline.

## Develop

```
uv run --no-project --with pytest python -m pytest -q tests/
uvx ruff check numwatch.py tests/
```

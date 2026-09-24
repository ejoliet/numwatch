# numwatch

> Watch exactly one number — from a shell command, URL, or SQL query — over time. Sparkline, threshold, Slack ping. The anti-Grafana.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![License](https://img.shields.io/badge/core-MIT-lightgrey)
![Status](https://img.shields.io/badge/status-spike-orange)

---

## Purpose

**Problem**: 90% of monitoring needs are "watch this one number and tell me when it's weird." The available answers are either a full observability stack (Prometheus/Grafana, VictoriaMetrics, Splunk) or nothing (`cron + curl + if`). Host monitors (Beszel, Netdata) watch fixed system metrics only. changedetection.io extracts numbers from URLs but cannot run a shell command or hit a database.

**Solution**: A cron-invoked Python CLI. Config is one TOML file. State is one SQLite file. Output is one static HTML status page (strip-chart aesthetic, see `numwatch-preview.html`) plus Slack pings on threshold breach and recovery.

**Positioning sentence**: arbitrary source (shell / SQL / URL) → one float → SQLite → sparkline → threshold state machine → Slack. Nothing on the market does this end-to-end. Do not compete on URL-watching — changedetection.io owns that. Lead with shell and SQL.

**Scope**: single-user, single-host, runs anywhere cron runs. First dogfood targets: Jenkins big-executor CPU, ROMAN-4652 light-curve row count.

---

## Locked Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python 3.11+ | Fast spike; binary packaging deferred (see Open Questions) |
| Runtime model | Cron-invoked (`numwatch tick`), no daemon | Simpler, survives reboots for free, matches anti-Grafana thesis |
| Business model | OSS core (MIT) + paid Pro extras | Same playbook as daggate; Ed25519 offline license via Lemon Squeezy |

---

## Recommended Stack

Thesis: **zero-dependency core**. `pip install numwatch` pulls nothing. This is a marketable feature and a maintenance invariant, so popularity research applies only where stdlib is insufficient.

| Layer | Chosen | Rejected | Why |
|-------|--------|----------|-----|
| Config parsing | `tomllib` (stdlib 3.11+) | tomli, pydantic-settings | Stdlib, read-only TOML is all we need |
| Storage | `sqlite3` (stdlib) | DuckDB, TinyDB, flat CSV | Stdlib, concurrent-safe enough for cron, trivially backed up |
| CLI | `argparse` (stdlib) | typer, click | Zero-dep invariant; 6 subcommands don't justify a framework |
| HTTP (webhook, url source) | `urllib.request` (stdlib) | requests, httpx | Zero-dep invariant; POST + GET only |
| Sparkline / page | Hand-rolled SVG in one HTML template | matplotlib, plotly | Zero-dep; design target already exists (`numwatch-preview.html`) |
| SQL sources (Pro) | `duckdb`, `psycopg` as optional extras | SQLAlchemy | Installed only via `pip install numwatch[pro]`; core stays clean |

> 💡 If `argparse` ergonomics hurt past the spike, revisit typer at packaging time — not before.

---

## Architecture

```
cron (* * * * *)
  └── numwatch tick
        ├── load numwatch.toml (tomllib)
        ├── for each watch due (now - last_sample >= every):
        │     ├── run source (shell | url | sql) with timeout → float | error
        │     ├── INSERT sample → numwatch.db (sqlite3)
        │     ├── evaluate rule → alert state machine
        │     │     └── on OK→FIRING or FIRING→OK transition: POST Slack webhook
        │     └── prune samples beyond retention
        └── regenerate status.html (static, atomic write)
```

**Alert state machine** (per watch, persisted in SQLite):

| State | Meaning | Transition |
|-------|---------|-----------|
| `ok` | Rule not matched | Match → `pending`, streak=1 |
| `pending` | Matched, streak < N | Match → streak+1; streak==N → `firing` + **breach ping** (once). No match → `ok` |
| `firing` | Alerted | No match → `ok` + **recovery ping** (once) |

> ⚠️ Ping exactly once per transition. Repeated pings while `firing` is the #1 way to get muted and churned.

---

## Repository Layout

```
numwatch/
├── numwatch.py            # Spike Gate 1–2: the ENTIRE tool, single file, ≤200 lines
├── numwatch.toml.example
├── tests/
│   └── test_numwatch.py   # State machine + rule parser unit tests
├── README.md              # This file
└── LICENSE                # MIT (core)
```

Post-gate-3 (packaging phase, not now): split into `src/numwatch/{cli,sources,rules,store,page}.py`, add `pyproject.toml`, `pro/` module with license check.

---

## Configuration Reference

### numwatch.toml (committed-safe, no secrets)

```toml
[watch.jenkins-cpu]
source   = "shell"
cmd      = "aws cloudwatch get-metric-statistics ... | jq -r .avg"
every    = "60s"
alert_if = "> 85 for 3"
notify   = "jenkins-alerts"          # alias, resolved from secrets file
unit     = "%"

[watch.lightcurve-rows]
source   = "shell"
cmd      = "duckdb lc.db -c 'select count(*) from lightcurves' -json | jq -r '.[0].c'"
every    = "10m"
alert_if = "stalls for 1h"           # Pro rule
```

### Secrets (never committed)

Webhook URLs live in `~/.config/numwatch/notify.toml` (chmod 600) or env var, never in `numwatch.toml`:

```toml
# ~/.config/numwatch/notify.toml
[slack]
jenkins-alerts = "https://hooks.slack.com/services/XXX/YYY/ZZZ"
```

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| `NUMWATCH_CONFIG` | path | `./numwatch.toml` | | Config file location |
| `NUMWATCH_DB` | path | `./numwatch.db` | | SQLite file |
| `NUMWATCH_NOTIFY` | path | `~/.config/numwatch/notify.toml` | | Webhook alias file |
| `NUMWATCH_SLACK_WEBHOOK` | url | — | | Fallback single webhook; overrides alias file |

> ⚠️ Standing invariant (cullroom incident): no secret, token, or webhook URL ever enters a committed file. Run ship-check before any repo goes public.

---

## Interface Contract

### CLI

```
Usage: numwatch [COMMAND]

Commands:
  init        Scaffold numwatch.toml + empty DB
  tick        Run all due watches (the cron entry point; cheap no-op when none due)
  run NAME    Force one watch now, print value
  status      Terminal table: name, unicode sparkline (▁▂▄▆█), value, state
  page        Regenerate status.html without sampling
  ls          List configured watches
```

Cron install: `* * * * * cd /path/to && numwatch tick >> tick.log 2>&1`

### Rule grammar

| Rule | Tier | Semantics |
|------|------|-----------|
| `"> X for N"` / `"< X for N"` | Core | Value beyond threshold for N consecutive samples |
| `">= X for N"` / `"<= X for N"` | Core | Inclusive variants |
| `"stalls for T"` | Pro | Value unchanged (or no successful sample) for duration T — ingest/heartbeat detection |
| `"drops P% in T"` / `"rises P% in T"` | Pro | Relative change vs. value T ago |

### Sources

| Source | Tier | Contract |
|--------|------|----------|
| `shell` | Core | `cmd` stdout, stripped, parsed as float. Timeout 30s default. Non-float → error sample |
| `url` | Pro | GET + optional `json_path` (dot/bracket path, no jq dependency) |
| `sql` | Pro | `driver = "duckdb" \| "postgres"`, query must return one row, one numeric column |

---

## Data Model

```sql
CREATE TABLE samples (
    watch  TEXT    NOT NULL,
    ts     INTEGER NOT NULL,          -- unix epoch seconds
    value  REAL,                      -- NULL = source error
    error  TEXT,
    PRIMARY KEY (watch, ts)
);

CREATE TABLE alert_state (
    watch          TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'ok',   -- ok | pending | firing
    streak         INTEGER NOT NULL DEFAULT 0,
    last_fired_ts  INTEGER
);
```

Retention: keep newest 10,000 samples per watch (configurable later); prune inside `tick`.

---

## Error Handling

| Error | When | Behavior |
|-------|------|----------|
| `ConfigError` | Bad TOML, unknown rule grammar | Exit 1, message to stderr; `tick` refuses to run |
| Source error | Timeout, non-zero exit, non-float output | Record error sample (value NULL); 3 consecutive → error ping (once) |
| Notify error | Webhook POST fails | Log to stderr, retry once next tick while transition unsent; never crash the tick |
| DB locked | Concurrent tick overlap | `BEGIN IMMEDIATE` + busy_timeout 5s; skip watch if still locked |

---

## OSS Core vs. Pro Split

| Capability | Core (MIT) | Pro ($39 one-time) |
|------------|-----------|--------------------|
| `shell` source | ✅ | |
| Threshold rules (`>`, `<` for N) | ✅ | |
| SQLite history + retention | ✅ | |
| Static HTML page + terminal sparklines | ✅ | |
| Slack webhook (breach + recovery) | ✅ | |
| `url` + `sql` sources | | ✅ |
| `stalls` + percent-change rules | | ✅ |
| Multi-channel notify (Discord, email, generic webhook) | | ✅ |
| Ed25519 offline license, Lemon Squeezy | | ✅ |

> ⚠️ Known risk: `shell` can emulate every Pro source (`curl`, `duckdb -c`). Pro therefore sells the **rules engine** (stall/delta detection is the annoying part to hand-roll) and convenience, not raw capability. Do not cripple core to fix this — a strong free tier is the funnel.

---

## Spike Gates

Spike-first. Each gate is GO/NO-GO before more work.

| Gate | Deliverable | Done when | NO-GO signal |
|------|-------------|-----------|--------------|
| 1 | `numwatch.py` ≤200 lines: shell → float → SQLite → `status.html` with SVG sparkline | Real Jenkins CPU sparkline renders from real samples | Can't stay under ~200 lines without fighting stdlib |
| 2 | Rule parser + state machine + Slack ping | Breach ping + recovery ping fire exactly once each against a test channel; state machine unit tests pass | Dedup logic gets hairy → rethink state model before continuing |
| 3 | Dogfood: 7 days on cron, watching jenkins-cpu + lightcurve-rows | Zero missed breaches, zero duplicate pings, page always current | Ping fatigue or missed events → fix before any packaging |

Only after Gate 3: packaging, Pro module, pricing page, ship-check.

---

## Non-Goals (v1)

- Push ingestion (numwatch pulls; if a number can't be pulled, it's out of scope)
- Daemon mode, multi-user, auth, TLS — the HTML page is a local file; hosting it is the user's problem
- Dashboards, aggregations, multiple numbers per watch — that road ends at rebuilding Grafana
- Windows support
- Anomaly detection / ML — maybe never

---

## Open Questions

- [ ] **Q1**: Ping on error samples in core, or Pro-only? Lean: core gets a plain "source failing" ping — silence on breakage is worse than a smaller Pro list. — owner: Emmanuel
- [ ] **Q2**: `status.html` default location: alongside DB, or `~/.local/share/numwatch/`? Lean: alongside DB, one directory to rsync.
- [ ] **Q3**: Post-spike packaging: stay pure Python (pipx) or ship a PyInstaller/Go rewrite binary? Defer to Gate 3 retro.

---

## Agent Build Instructions

> Implement Gates 1–2 only, from this README. Resolve Open Questions with the stated leans if Emmanuel hasn't answered.

### Constraints

- Python 3.11+, stdlib only (`tomllib`, `sqlite3`, `argparse`, `urllib.request`, `subprocess`)
- Single file `numwatch.py` for the spike; typed signatures on public functions
- Secrets only via env var or `~/.config/numwatch/notify.toml`; never read from `numwatch.toml`
- `AIDEV-` prefixed comments for non-obvious decisions (state machine transitions, atomic HTML write)
- Atomic writes: `status.html` via temp file + `os.replace`
- Tests: state machine and rule parser covered in `tests/test_numwatch.py`; no network, no real Slack

### Acceptance Criteria (spike)

- [ ] Gate 1 demo: `numwatch init && numwatch run jenkins-cpu && numwatch page` produces a rendering `status.html`
- [ ] Gate 2 demo: simulated breach fires exactly one Slack POST (mocked in tests, real once manually)
- [ ] `numwatch.py` ≤ 200 lines at Gate 1 (state machine may push Gate 2 to ~300; note the count)
- [ ] `python -m pytest` passes; `ruff check` clean
- [ ] No secret appears in any committed file

---

## Next Steps

1. [ ] Emmanuel answers Q1–Q2 (or accepts leans)
2. [ ] Agent builds Gate 1; GO/NO-GO review
3. [ ] Agent builds Gate 2; GO/NO-GO review
4. [ ] Install cron on a real host; start Gate 3 dogfood clock (7 days)
5. [ ] Gate 3 retro: decide Q3 (packaging), then Pro module + ship-check + Lemon Squeezy

---

## References

- `numwatch-preview.html` — design target for the status page (strip-chart aesthetic)
- Competitor scan (2026-08-08): changedetection.io conditional number rules — https://changedetection.io/tutorial/conditional-actions-web-page-changes
- Prior art for the split-pricing playbook: daggate ($39 Gumroad)
- Emmanuel skills applied: `readme-driven-dev`, `emmanuel-markdown`, `ship-check` (pre-release), `emmanuel-engineering`

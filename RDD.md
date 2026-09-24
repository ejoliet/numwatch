# numwatch

> Watch exactly one number — from a shell command, URL, or SQL query — over time. Sparkline, threshold, Slack ping. The anti-Grafana.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![License](https://img.shields.io/badge/core-MIT-lightgrey)
![Status](https://img.shields.io/badge/status-gate%203%20dogfood-yellow)

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
├── numwatch.py              # Gates 1–2: the ENTIRE tool, single file (487 lines at Gate 2 GO)
├── numwatch.toml            # Local watches (currently mac test watches; swap for dogfood targets)
├── numwatch.toml.example    # Committed-safe example (jenkins-cpu, lightcurve-rows)
├── numwatch-preview.html    # Design target for status.html
├── test.sh                  # Manual breach/recovery drive against a real webhook
├── tests/
│   └── test_numwatch.py     # Rule parser, duration, state machine, notify, sample_watch e2e
├── ruff.toml                # target-version = "py311" (keeps tomllib sorted as stdlib)
├── .gitignore               # *.db, status.html, tick.log, __pycache__, .omc/, .claude/
├── implementation-notes.md  # One-line decision log; grep for DEVIATION:
├── RDD.md                   # This file: spec + gate status + agent instructions
└── README.md                # Public-facing summary
```

Missing, add during Phase 4: `LICENSE` (MIT, core), `pyproject.toml`.

Phase 4 (packaging, after Gate 3 GO): split into `src/numwatch/{cli,sources,rules,store,page}.py`, add `pyproject.toml`, `pro/` module with license check. See Agent Build Instructions.

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

| Gate | Deliverable | Done when | NO-GO signal | Status |
|------|-------------|-----------|--------------|--------|
| 1 | `numwatch.py` ≤200 lines: shell → float → SQLite → `status.html` with SVG sparkline | Real Jenkins CPU sparkline renders from real samples | Can't stay under ~200 lines without fighting stdlib | ✅ GO 2026-09-23 (sparkline renders from `random` watch; Jenkins CPU pending Gate 3) |
| 2 | Rule parser + state machine + Slack ping | Breach ping + recovery ping fire exactly once each against a test channel; state machine unit tests pass | Dedup logic gets hairy → rethink state model before continuing | ✅ GO 2026-09-23 (27 tests, e2e exactly-once test; real Slack ping still to run via `test.sh`) |
| 3 | Dogfood: 7 days on cron, watching jenkins-cpu + lightcurve-rows | Zero missed breaches, zero duplicate pings, page always current | Ping fatigue or missed events → fix before any packaging | ⏳ Not started. Blocked on real `cmd` for both watches + notify alias (Emmanuel) |

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

- [x] **Q1**: Resolved with lean (2026-09-23). Core pings "source FAILING" once after 3 consecutive error samples (`ERROR_STREAK_THRESHOLD`), and "source RECOVERED" once. Same `step_state` dedup path as rule breaches.
- [x] **Q2**: Resolved with lean (2026-09-23). `status.html` written next to `numwatch.db`, both cwd-relative; override via `NUMWATCH_PAGE` / `NUMWATCH_DB`.
- [ ] **Q3**: Post-spike packaging: stay pure Python (pipx) or ship a PyInstaller/Go rewrite binary? Decide at Gate 3 retro. Lean: pure Python + pipx; zero-dep core makes this painless.
- [ ] **Q4**: Line budget. `numwatch.py` is 487 lines vs. the ~300 estimate. Accept as-is until the Phase 4 split, or trim first? Lean: accept; the split resolves it.

---

## Agent Build Instructions

> Gates 1–2 are DONE. Current phase: **Gate 3 prep + dogfood**. Do not start Phase 4 or Pro work until Gate 3 is marked GO in the table above. Resolve Open Questions with the stated leans if Emmanuel hasn't answered. Log every non-obvious decision in `implementation-notes.md`.

### Verify before touching anything

```
uv run --no-project --with pytest python -m pytest -q tests/   # expect 27 passed
uvx ruff check numwatch.py tests/                               # expect "All checks passed!"
git grep -nE 'hooks\.slack\.com/services/' -- . | grep -v XXX # expect no output
```

pytest is not installed system-wide; `uv` is. Do not add a dev dependency to fix that.

### Constraints

- Python 3.11+, stdlib only (`tomllib`, `sqlite3`, `argparse`, `urllib.request`, `subprocess`)
- Single file `numwatch.py` for the spike; typed signatures on public functions
- Secrets only via env var or `~/.config/numwatch/notify.toml`; never read from `numwatch.toml`
- `AIDEV-` prefixed comments for non-obvious decisions (state machine transitions, atomic HTML write)
- Atomic writes: `status.html` via temp file + `os.replace`
- Tests: state machine and rule parser covered in `tests/test_numwatch.py`; no network, no real Slack

### Acceptance Criteria (spike)

- [x] Gate 1 demo: `numwatch init && numwatch run example && numwatch page` produces a rendering `status.html` (2026-09-23)
- [x] Gate 2 demo: simulated breach fires exactly one Slack POST, mocked (`test_sample_watch_breach_and_recovery_ping_exactly_once`)
- [ ] Gate 2 demo, real: one manual breach + recovery ping to a test channel via `./test.sh` (needs `NUMWATCH_SLACK_WEBHOOK` exported; Emmanuel)
- [x] Line count noted: 487 at Gate 2 (see Q4)
- [x] `pytest` passes (27); `ruff check` clean (ruff 0.16.8, `ruff.toml` pins py311)
- [x] No secret appears in any committed file

### Gate 3 instructions (current phase)

Agent can do without Emmanuel:

1. Add `LICENSE` (MIT, copyright Emmanuel Joliet, 2026).
2. Keep `numwatch.toml.example` as the dogfood template. Do not put real Jenkins/DuckDB commands in it if they reveal hostnames or paths that should not be public.
3. Do not edit `numwatch.py` during the dogfood window except for bugs found by the dogfood. Every fix: failing test first, then fix, then entry in `implementation-notes.md`.
4. During dogfood, when asked to "check numwatch": run `./numwatch.py status`, inspect `tick.log` tail, and `sqlite3 numwatch.db "select watch, count(*), max(ts) from samples group by watch"`. Report gaps > 2× `every` as missed samples.

Needs Emmanuel:

1. Real `cmd` for `jenkins-cpu` and `lightcurve-rows` in `numwatch.toml` (local file; `numwatch.toml` is committed today, so either keep commands non-sensitive or add it to `.gitignore` and rely on the example).
2. `~/.config/numwatch/notify.toml` with the `jenkins-alerts` alias, `chmod 600`.
3. Run `./test.sh` once for the real Slack ping (closes the remaining Gate 2 checkbox).
4. Install cron and note the start date here:

```
* * * * * cd /Users/ejoliet/devspace/ejoliet/numwatch && ./numwatch.py tick >> tick.log 2>&1
```

Gate 3 dogfood start: `____-__-__` · planned end (7 days): `____-__-__`

Gate 3 GO/NO-GO checklist (fill at retro):

- [ ] Zero missed breaches (compare Slack history with `samples` table)
- [ ] Zero duplicate pings (`last_fired_ts` transitions match Slack message count)
- [ ] `status.html` mtime never older than 2 min while cron ran
- [ ] `tick.log` free of tracebacks
- [ ] Q3 decided; Q4 decided

### Phase 4: packaging (only after Gate 3 GO)

Constraints stay: zero-dep core, Python 3.11+, tests must pass unchanged in behaviour.

1. Split `numwatch.py` into `src/numwatch/{cli,sources,rules,store,page}.py` plus `__init__.py`; keep one public surface per module (`run_shell`, `parse_rule`/`step_state`, `init_db`/`insert_sample`/..., `render_page`). No new abstractions; move code, do not redesign.
2. `pyproject.toml`: name `numwatch`, `requires-python = ">=3.11"`, no runtime dependencies, console script `numwatch = numwatch.cli:main`, optional extra `pro = ["duckdb", "psycopg[binary]"]`. Build backend: hatchling or setuptools, whichever needs fewer lines.
3. Move `ruff.toml` content into `[tool.ruff]`; delete `ruff.toml`.
4. Update tests to import from the package; keep `tests/test_numwatch.py` as the single file until it exceeds ~400 lines.
5. Verify `pipx install .` then `numwatch --help`, `numwatch init`, `numwatch tick` in an empty temp dir.
6. Update `README.md` install section and the Repository Layout in this file.
7. Delete root `numwatch.py` only after the console script is verified. Update the cron line.

### Phase 5: Pro module (after Phase 4)

Sells the rules engine and convenience, not raw capability (see OSS Core vs. Pro Split). Order by value:

1. Rules: `stalls for T` (no change or no successful sample for duration T), `drops P% in T`, `rises P% in T`. Extend `parse_rule` and `rule_matches`; the alert state machine stays untouched. Unit tests for each rule against synthetic sample series.
2. Sources: `url` (GET + dot/bracket `json_path`, stdlib only), `sql` (`driver = "duckdb" | "postgres"`, one row, one numeric column; import driver lazily and raise `ProFeatureError` with the `pip install numwatch[pro]` hint if missing).
3. Notify: Discord, generic webhook, email (`smtplib`). Aliases resolved from `notify.toml` sections `[discord]`, `[webhook]`, `[email]`.
4. License: Ed25519 offline check (`cryptography` is not stdlib; evaluate a pure-Python verifier vs. making it a Pro extra). Key file at `~/.config/numwatch/license.key`. Missing or invalid key: Pro features raise `ProFeatureError`; core keeps working. Lemon Squeezy issues the key.
5. `pro/` lives inside the same package, gated by the license check. Do not cripple core.

### Phase 6: ship

1. Run `ship-check` skill: secrets scan, README quickstart works from a fresh clone, LICENSE present.
2. Pricing page copy: one-time $39, what Pro adds, refund policy.
3. Tag `v0.1.0`, publish to PyPI, announce.

---

## Next Steps

1. [x] Q1–Q2 resolved with leans (2026-09-23)
2. [x] Gate 1 built and GO (2026-09-23)
3. [x] Gate 2 built and GO (2026-09-23); real Slack ping via `./test.sh` still owed
4. [ ] Commit the Gate 2 state (ruff fixes, `tests/` move, e2e test, `ruff.toml`, `.gitignore`, `implementation-notes.md`)
5. [ ] Agent: add `LICENSE` (MIT)
6. [ ] Emmanuel: real `cmd` for `jenkins-cpu` + `lightcurve-rows`, `notify.toml` alias, install cron, write the start date in the Gate 3 section
7. [ ] 7-day dogfood; agent runs the "check numwatch" routine on request; bugs fixed test-first
8. [ ] Gate 3 retro: fill checklist, decide Q3 + Q4, mark Gate 3 in the table
9. [ ] Phase 4 packaging (agent), then Phase 5 Pro (agent), then Phase 6 ship (Emmanuel + `ship-check`)

---

## References

- `numwatch-preview.html` — design target for the status page (strip-chart aesthetic)
- Competitor scan (2026-08-08): changedetection.io conditional number rules — https://changedetection.io/tutorial/conditional-actions-web-page-changes
- Prior art for the split-pricing playbook: daggate ($39 Gumroad)
- Emmanuel skills applied: `readme-driven-dev`, `emmanuel-markdown`, `ship-check` (pre-release), `emmanuel-engineering`

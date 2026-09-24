# CLAUDE.md

numwatch: watch one number (shell cmd, URL, SQL) over time. Cron-invoked Python CLI, one TOML config, one SQLite file, one static `status.html`, Slack ping on breach/recovery. No daemon.

`RDD.md` is the spec and source of truth: locked decisions, gate table, phase checklists, open questions. Read its "Agent Build Instructions" section before any non-trivial change. This file only covers what an agent needs every session.

## Current phase

Gates 1–3 GO. Phase 4 (packaging) is next for agents. Gate 3b (7-day real-target cron run) runs in parallel, owned by Emmanuel, non-blocking. Check `RDD.md` gate table and Next Steps for live status; do not trust this paragraph if they disagree.

## Layout

- `numwatch.py` — whole app, single file (~490 lines) until Phase 4 splits it into `src/numwatch/`.
- `tests/test_numwatch.py` — all tests (state machine, rule parser, e2e with mocked notify). Single file until ~400 lines.
- `examples/` — secret-free dogfood harness (`demo_service.py` fake metric + mock Slack, `dogfood.sh`). Output lands in `examples/.dogfood/` (gitignored).
- `numwatch.toml` — committed local watch config (mac-cpu, mac-battery, random). `numwatch.toml.example` is the public template.
- `implementation-notes.md` — one line per non-obvious decision; `DEVIATION:` prefix for conservative choices made without asking.
- `test.sh` — manual real-Slack breach/recovery run; needs `NUMWATCH_SLACK_WEBHOOK` exported.

## Commands

```
uv run --no-project --with pytest python -m pytest -q tests/   # expect all pass (27 at Gate 2)
uvx ruff check numwatch.py tests/                               # expect "All checks passed!"
git grep -nE 'hooks\.slack\.com/services/' -- . | grep -v XXX   # expect no output
./examples/dogfood.sh 300 5                                     # 5-min local dogfood -> GATE3 DOGFOOD: PASS|FAIL
./numwatch.py init | run <watch> | status | page | tick
```

pytest is not installed system-wide; use `uv`. Do not add a dev dependency to fix that.

## Hard rules

- Python 3.11+, stdlib only for core (`tomllib`, `sqlite3`, `argparse`, `urllib.request`, `subprocess`). No new dependencies without explicit request.
- No secret, token, or webhook URL in any committed file. Webhooks come only from `~/.config/numwatch/notify.toml` (chmod 600) or `NUMWATCH_SLACK_WEBHOOK`. Localhost URLs in `examples/` are fine.
- Do not create `LICENSE`. Emmanuel adds it himself.
- Bug fix flow: failing test first, then fix, then one line in `implementation-notes.md`.
- Every non-obvious decision or deviation gets a line in `implementation-notes.md`, dated.
- `AIDEV-` prefixed comments for non-obvious code decisions (state machine transitions, atomic HTML write).
- `status.html` written atomically (temp file + `os.replace`). Keep it that way.
- Open Questions in `RDD.md` have stated leans. Take the lean if Emmanuel hasn't answered; log it.
- Phase 4 split is "move code, do not redesign". No new abstractions.
- `ruff.toml` pins `target-version = "py311"` so `tomllib` sorts as stdlib. Don't remove until it moves into `pyproject.toml` (Phase 4 step 3).

## Gotchas

- `notify` is a module global looked up at call time: monkeypatch `numwatch.notify` in tests. `_http_post_urllib` is bound as a default arg at def time, patching it does nothing.
- Error samples store `value = None`. `rule_matches` never matches `None`; only the source-error track (FAILING/RECOVERED after `ERROR_STREAK_THRESHOLD` consecutive errors) pings for a failing source.
- `step_state` is the single dedup point for pings. Do not add a second one.
- Paths are cwd-relative by default; override with `NUMWATCH_CONFIG`, `NUMWATCH_DB`, `NUMWATCH_NOTIFY`, `NUMWATCH_PAGE`.
- "Check numwatch" during dogfood means: `./numwatch.py status`, tail `tick.log`, and `sqlite3 numwatch.db "select watch, count(*), max(ts) from samples group by watch"`. Report gaps > 2× `every` as missed samples.

## Commits

Lore protocol (see `~/dev/ejoliet/AGENTS.md`): intent line says why, trailers `Constraint:` / `Rejected:` / `Tested:` / `Not-tested:` where they add value. Commit only when asked.

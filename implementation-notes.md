# implementation-notes

One line per entry. Grep-friendly. `DEVIATION:` marks a conservative choice made without asking.

- 2026-09-23 Gate 1 verified: `init && run example x3 && page` renders status.html with SVG polyline (scratchpad run).
- 2026-09-23 Gate 2 verified: 27 tests pass via `uv run --no-project --with pytest python -m pytest -q tests/`; pytest not installed system-wide, uv used instead of adding a dev dependency.
- 2026-09-23 Moved test_numwatch.py to tests/ to match RDD layout; its existing sys.path hack (parents[1]) already assumed that location.
- 2026-09-23 Added tests/test_sample_watch_breach_and_recovery_ping_exactly_once: monkeypatches numwatch.notify (module global, looked up at call time) rather than _http_post_urllib (bound as default arg at def time, so patching it would not take effect).
- 2026-09-23 ruff clean under ruff 0.16.8 defaults: applied autofixes (UP045 Optional -> X | None, I001, UP035), added check=False to subprocess.run (PLW1510), chmod +x numwatch.py (EXE001), underscored unused unpacked vars (RUF059).
- 2026-09-23 Q1 accepted lean: core pings once after 3 consecutive source errors (ERROR_STREAK_THRESHOLD), reusing step_state as the single dedup point.
- 2026-09-23 Q2 accepted lean: status.html written next to numwatch.db (both cwd-relative, overridable via NUMWATCH_PAGE / NUMWATCH_DB).
- 2026-09-23 DEVIATION: did not install the Gate 3 crontab entry. Dogfood targets (jenkins-cpu, lightcurve-rows) need real commands and a webhook alias in ~/.config/numwatch/notify.toml that only Emmanuel can supply; numwatch.toml currently holds mac-cpu/mac-battery/random test watches.
- 2026-09-23 numwatch.py is 487 lines vs RDD's ~300 estimate for Gate 2. Not trimmed: RDD says note the count, not fight it before Gate 3 retro.
- 2026-09-23 Added ruff.toml with target-version = "py311": without it ruff assumes py39 and sorts `import tomllib` as third-party (not stdlib before 3.11), moving it below the stdlib block.
- 2026-09-24 RDD.md updated: gate table gained Status column (Gates 1-2 GO, Gate 3 not started), Q1/Q2 marked resolved, Q4 added (line budget), Agent Build Instructions rewritten as Gate 3 / Phase 4 packaging / Phase 5 Pro / Phase 6 ship checklists. README.md gained Status, Quickstart, Develop.
- 2026-09-24 DEVIATION: LICENSE not created; copyright holder name is Emmanuel's call. Listed as Next Step 5 with the assumed holder so the next agent can add it in one line.

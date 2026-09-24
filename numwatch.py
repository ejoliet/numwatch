#!/usr/bin/env python3
"""numwatch — watch one number over time. Sparkline, threshold, Slack ping.

Gate 1: shell source -> SQLite -> status.html sparkline.
Gate 2: rule parser + dedup alert state machine + Slack ping.

Stdlib only. See README.md (numwatch-README.md) for the full spec.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = Path(os.environ.get("NUMWATCH_CONFIG", "numwatch.toml"))
DEFAULT_DB = Path(os.environ.get("NUMWATCH_DB", "numwatch.db"))
DEFAULT_NOTIFY = Path(
    os.environ.get("NUMWATCH_NOTIFY", str(Path.home() / ".config/numwatch/notify.toml"))
)
DEFAULT_PAGE = Path(os.environ.get("NUMWATCH_PAGE", "status.html"))
ERROR_STREAK_THRESHOLD = 3  # Q1 lean: core pings on 3 consecutive source errors


class ConfigError(Exception):
    pass


class ProFeatureError(Exception):
    pass


class SourceError(Exception):
    pass


# ---------------------------------------------------------------- parsing --

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_DURATION_MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> int:
    m = _DURATION_RE.match(s.strip())
    if not m:
        raise ConfigError(f"bad duration {s!r}, expected e.g. '60s', '10m', '1h'")
    n, unit = m.groups()
    return int(n) * _DURATION_MULT[unit]


@dataclass(frozen=True)
class Rule:
    op: str  # '>', '<', '>=', '<='
    threshold: float
    n: int


_THRESHOLD_RE = re.compile(r"^(>=|<=|>|<)\s*([-\d.]+)\s+for\s+(\d+)$")
_PRO_RULE_RE = re.compile(r"^(stalls\s+for\s+|drops\s+\d|rises\s+\d)")


def parse_rule(s: str) -> Rule:
    s = s.strip()
    m = _THRESHOLD_RE.match(s)
    if m:
        op, threshold, n = m.groups()
        return Rule(op, float(threshold), int(n))
    if _PRO_RULE_RE.match(s):
        raise ProFeatureError(f"rule {s!r} requires numwatch Pro (stalls/drops/rises)")
    raise ConfigError(f"unrecognized rule: {s!r}")


def rule_matches(rule: Rule, value: float | None) -> bool:
    # AIDEV-note: a None value (source error) never matches a threshold rule.
    # Error detection is a separate track (see step_state on error_state).
    if value is None:
        return False
    ops: dict[str, Callable[[float, float], bool]] = {
        ">": lambda v, t: v > t,
        "<": lambda v, t: v < t,
        ">=": lambda v, t: v >= t,
        "<=": lambda v, t: v <= t,
    }
    return ops[rule.op](value, rule.threshold)


# ------------------------------------------------------------ state machine --

def step_state(state: str, streak: int, matched: bool, n: int) -> tuple[str, int, str | None]:
    """One dedup'd transition step. Returns (new_state, new_streak, transition).

    transition is None, 'breach' (fires once entering 'firing'), or
    'recovery' (fires once leaving 'firing'). AIDEV-note: this is the single
    dedup point for both rule breaches and source-error streaks (reused with
    n=ERROR_STREAK_THRESHOLD) — do not special-case pinging anywhere else.
    """
    if matched:
        if state in ("ok", "pending"):
            new_streak = streak + 1
            if new_streak >= n:
                return "firing", new_streak, "breach"
            return "pending", new_streak, None
        return "firing", streak, None  # already firing, no repeat ping
    else:
        if state == "firing":
            return "ok", 0, "recovery"
        return "ok", 0, None


# --------------------------------------------------------------------- io --

def run_shell(cmd: str, timeout: int = 30) -> float:
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise SourceError(f"timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise SourceError((proc.stderr or f"exit {proc.returncode}").strip()[:300])
    out = proc.stdout.strip()
    try:
        return float(out)
    except ValueError:
        raise SourceError(f"non-numeric output: {out[:100]!r}")


def _http_post_urllib(url: str, payload: bytes) -> None:
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def notify(webhook_url: str, text: str, http_post: Callable[[str, bytes], None] = _http_post_urllib) -> None:
    # AIDEV-note: best-effort. A failed Slack POST must never crash a tick —
    # a missed ping is bad, a crashed cron job that stops sampling is worse.
    try:
        http_post(webhook_url, json.dumps({"text": text}).encode())
    except Exception as e:  # noqa: BLE001 - deliberately broad, see note above
        print(f"numwatch: notify failed: {e}", file=sys.stderr)


def resolve_webhook(alias: str | None, notify_map: dict) -> str | None:
    env = os.environ.get("NUMWATCH_SLACK_WEBHOOK")
    if env:
        return env
    if alias:
        return notify_map.get("slack", {}).get(alias)
    return None


# ---------------------------------------------------------------- config --

@dataclass
class Watch:
    name: str
    source: str
    cmd: str
    every: int
    rule: Rule
    notify_alias: str | None
    unit: str


def load_config(path: Path) -> dict[str, Watch]:
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    watches: dict[str, Watch] = {}
    for name, w in raw.get("watch", {}).items():
        source = w.get("source", "shell")
        if source != "shell":
            raise ProFeatureError(f"watch {name!r}: source {source!r} requires numwatch Pro")
        if "cmd" not in w:
            raise ConfigError(f"watch {name!r}: missing 'cmd'")
        watches[name] = Watch(
            name=name,
            source=source,
            cmd=w["cmd"],
            every=parse_duration(w.get("every", "60s")),
            rule=parse_rule(w["alert_if"]) if "alert_if" in w else Rule(">", float("inf"), 1),
            notify_alias=w.get("notify"),
            unit=w.get("unit", ""),
        )
    return watches


def load_notify_map(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# ------------------------------------------------------------------- store --

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    watch TEXT NOT NULL,
    ts    INTEGER NOT NULL,
    value REAL,
    error TEXT,
    PRIMARY KEY (watch, ts)
);
CREATE TABLE IF NOT EXISTS alert_state (
    watch         TEXT PRIMARY KEY,
    state         TEXT NOT NULL DEFAULT 'ok',
    streak        INTEGER NOT NULL DEFAULT 0,
    error_state   TEXT NOT NULL DEFAULT 'ok',
    error_streak  INTEGER NOT NULL DEFAULT 0,
    last_fired_ts INTEGER
);
"""


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def last_sample_ts(conn: sqlite3.Connection, watch: str) -> int | None:
    row = conn.execute(
        "SELECT MAX(ts) FROM samples WHERE watch = ?", (watch,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def insert_sample(conn: sqlite3.Connection, watch: str, ts: int, value: float | None, error: str | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO samples (watch, ts, value, error) VALUES (?, ?, ?, ?)",
        (watch, ts, value, error),
    )
    conn.commit()


def recent_samples(conn: sqlite3.Connection, watch: str, limit: int = 90) -> list[tuple[int, float | None]]:
    rows = conn.execute(
        "SELECT ts, value FROM samples WHERE watch = ? ORDER BY ts DESC LIMIT ?",
        (watch, limit),
    ).fetchall()
    return list(reversed(rows))


def get_alert_row(conn: sqlite3.Connection, watch: str) -> tuple[str, int, str, int]:
    row = conn.execute(
        "SELECT state, streak, error_state, error_streak FROM alert_state WHERE watch = ?",
        (watch,),
    ).fetchone()
    if row is None:
        conn.execute("INSERT INTO alert_state (watch) VALUES (?)", (watch,))
        conn.commit()
        return "ok", 0, "ok", 0
    return row


def set_alert_row(conn: sqlite3.Connection, watch: str, state: str, streak: int, error_state: str, error_streak: int, fired: bool) -> None:
    if fired:
        conn.execute(
            "UPDATE alert_state SET state=?, streak=?, error_state=?, error_streak=?, last_fired_ts=strftime('%s','now') WHERE watch=?",
            (state, streak, error_state, error_streak, watch),
        )
    else:
        conn.execute(
            "UPDATE alert_state SET state=?, streak=?, error_state=?, error_streak=? WHERE watch=?",
            (state, streak, error_state, error_streak, watch),
        )
    conn.commit()


def prune(conn: sqlite3.Connection, watch: str, keep: int = 10_000) -> None:
    conn.execute(
        """DELETE FROM samples WHERE watch = ? AND ts NOT IN (
               SELECT ts FROM samples WHERE watch = ? ORDER BY ts DESC LIMIT ?
           )""",
        (watch, watch, keep),
    )
    conn.commit()


# ------------------------------------------------------------------- tick --

def sample_watch(conn: sqlite3.Connection, w: Watch, notify_map: dict, now: int) -> None:
    try:
        value = run_shell(w.cmd)
        error = None
    except SourceError as e:
        value = None
        error = str(e)
    insert_sample(conn, w.name, now, value, error)

    state, streak, error_state, error_streak = get_alert_row(conn, w.name)

    # Rule breach/recovery track
    matched = rule_matches(w.rule, value)
    state, streak, transition = step_state(state, streak, matched, w.rule.n)

    # Source-error track (Q1 lean: core pings on repeated source failure)
    error_state, error_streak, error_transition = step_state(
        error_state, error_streak, error is not None, ERROR_STREAK_THRESHOLD
    )

    fired = transition is not None or error_transition is not None
    set_alert_row(conn, w.name, state, streak, error_state, error_streak, fired)

    webhook = resolve_webhook(w.notify_alias, notify_map)
    if webhook and transition:
        verb = "BREACH" if transition == "breach" else "RECOVERED"
        notify(webhook, f"numwatch: {w.name} {verb} — {value}{w.unit} ({w.rule.op} {w.rule.threshold} for {w.rule.n})")
    if webhook and error_transition:
        verb = "FAILING" if error_transition == "breach" else "RECOVERED"
        notify(webhook, f"numwatch: {w.name} source {verb}" + (f" — {error}" if error else ""))

    prune(conn, w.name)


def tick(config_path: Path, db_path: Path, notify_path: Path, page_path: Path, force: str | None = None) -> None:
    watches = load_config(config_path)
    notify_map = load_notify_map(notify_path)
    conn = init_db(db_path)
    now = int(__import__("time").time())
    for name, w in watches.items():
        if force is not None and name != force:
            continue
        if force is None:
            last = last_sample_ts(conn, name)
            if last is not None and now - last < w.every:
                continue
        sample_watch(conn, w, notify_map, now)
    render_page(conn, watches, page_path)
    conn.close()


# ------------------------------------------------------------------- page --

def _sparkline_svg(samples: list[tuple[int, float | None]], w: int = 300, h: int = 60) -> str:
    vals = [v for _, v in samples if v is not None]
    if not vals:
        return f'<svg viewBox="0 0 {w} {h}"><text x="4" y="{h//2}" font-size="10" fill="#888">no data</text></svg>'
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = len(samples)
    pts = []
    for i, (_, v) in enumerate(samples):
        if v is None:
            continue
        x = (i / max(n - 1, 1)) * (w - 4) + 2
        y = h - 4 - ((v - lo) / span) * (h - 8)
        pts.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="#16324F" stroke-width="2"/>'
        f"</svg>"
    )


def render_page(conn: sqlite3.Connection, watches: dict[str, Watch], page_path: Path) -> None:
    cards = []
    for name, w in watches.items():
        samples = recent_samples(conn, name)
        last_val = samples[-1][1] if samples else None
        state, _, error_state, _ = get_alert_row(conn, name)
        badge = "FIRING" if state == "firing" else ("ERROR" if error_state == "firing" else "ok")
        cards.append(
            f'<div class="card"><h2>{name}</h2>'
            f'<div class="val">{last_val if last_val is not None else "—"}{w.unit} '
            f'<span class="badge {badge}">{badge}</span></div>'
            f"{_sparkline_svg(samples)}</div>"
        )
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>numwatch</title><style>"
        "body{font-family:monospace;background:#EDF1EA;color:#16324F;padding:20px}"
        ".card{background:#F6F8F4;border:1px solid #16324F;padding:12px;margin-bottom:12px;max-width:340px}"
        ".val{font-size:28px;font-weight:bold}"
        ".badge{font-size:11px;padding:2px 6px;border:1px solid;margin-left:8px}"
        ".badge.ok{color:#2E6B4F;border-color:#2E6B4F}"
        ".badge.FIRING,.badge.ERROR{color:#C0392B;border-color:#C0392B}"
        "</style></head><body><h1>numwatch</h1>" + "".join(cards) + "</body></html>"
    )
    # AIDEV-note: atomic write so a concurrent page load never sees a half-written file.
    tmp = page_path.with_suffix(page_path.suffix + ".tmp")
    tmp.write_text(html)
    os.replace(tmp, page_path)


_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline_text(samples: list[tuple[int, float | None]]) -> str:
    vals = [v for _, v in samples if v is not None]
    if not vals:
        return "no data"
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    return "".join(
        _BLOCKS[min(7, int((v - lo) / span * 7))] if v is not None else " "
        for _, v in samples
    )


# -------------------------------------------------------------------- cli --

def cmd_init(args: argparse.Namespace) -> None:
    if not DEFAULT_CONFIG.exists():
        DEFAULT_CONFIG.write_text(
            '[watch.example]\n'
            'source   = "shell"\n'
            'cmd      = "echo $((RANDOM % 100))"\n'
            'every    = "60s"\n'
            'alert_if = "> 80 for 3"\n'
            'unit     = ""\n'
        )
        print(f"wrote {DEFAULT_CONFIG}")
    init_db(DEFAULT_DB).close()
    print(f"initialized {DEFAULT_DB}")


def cmd_tick(args: argparse.Namespace) -> None:
    tick(DEFAULT_CONFIG, DEFAULT_DB, DEFAULT_NOTIFY, DEFAULT_PAGE)


def cmd_run(args: argparse.Namespace) -> None:
    tick(DEFAULT_CONFIG, DEFAULT_DB, DEFAULT_NOTIFY, DEFAULT_PAGE, force=args.name)
    conn = init_db(DEFAULT_DB)
    samples = recent_samples(conn, args.name, limit=1)
    print(samples[-1][1] if samples else "no data")
    conn.close()


def cmd_status(args: argparse.Namespace) -> None:
    watches = load_config(DEFAULT_CONFIG)
    conn = init_db(DEFAULT_DB)
    for name, w in watches.items():
        samples = recent_samples(conn, name)
        state, _, error_state, _ = get_alert_row(conn, name)
        last_val = samples[-1][1] if samples else None
        flag = "!" if state == "firing" or error_state == "firing" else " "
        print(f"{flag} {name:24s} {_sparkline_text(samples):10s} {last_val}{w.unit}")
    conn.close()


def cmd_page(args: argparse.Namespace) -> None:
    watches = load_config(DEFAULT_CONFIG)
    conn = init_db(DEFAULT_DB)
    render_page(conn, watches, DEFAULT_PAGE)
    conn.close()
    print(f"wrote {DEFAULT_PAGE}")


def cmd_ls(args: argparse.Namespace) -> None:
    for name in load_config(DEFAULT_CONFIG):
        print(name)


def main() -> None:
    parser = argparse.ArgumentParser(prog="numwatch")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("tick").set_defaults(func=cmd_tick)
    p_run = sub.add_parser("run")
    p_run.add_argument("name")
    p_run.set_defaults(func=cmd_run)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("page").set_defaults(func=cmd_page)
    sub.add_parser("ls").set_defaults(func=cmd_ls)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ConfigError, ProFeatureError, SourceError) as e:
        print(f"numwatch: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

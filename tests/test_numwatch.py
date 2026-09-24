import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from numwatch import (
    ConfigError,
    ProFeatureError,
    Rule,
    notify,
    parse_duration,
    parse_rule,
    rule_matches,
    step_state,
)

# ---------------------------------------------------------- parse_duration --

@pytest.mark.parametrize(
    "s,expected",
    [("60s", 60), ("10m", 600), ("1h", 3600), ("7d", 604800), ("1s", 1)],
)
def test_parse_duration_valid(s, expected):
    assert parse_duration(s) == expected


@pytest.mark.parametrize("s", ["60", "60x", "m10", "", "10 m"])
def test_parse_duration_invalid(s):
    with pytest.raises(ConfigError):
        parse_duration(s)


# ---------------------------------------------------------------- parse_rule --

def test_parse_rule_threshold_gt():
    r = parse_rule("> 85 for 3")
    assert r == Rule(">", 85.0, 3)


def test_parse_rule_threshold_lte_negative():
    r = parse_rule("<= -5.5 for 1")
    assert r == Rule("<=", -5.5, 1)


def test_parse_rule_pro_stalls_raises():
    with pytest.raises(ProFeatureError):
        parse_rule("stalls for 1h")


def test_parse_rule_pro_drops_raises():
    with pytest.raises(ProFeatureError):
        parse_rule("drops 10% in 1h")


def test_parse_rule_garbage_raises_config_error():
    with pytest.raises(ConfigError):
        parse_rule("banana")


# --------------------------------------------------------------- rule_matches --

def test_rule_matches_gt():
    r = Rule(">", 85.0, 3)
    assert rule_matches(r, 90.0) is True
    assert rule_matches(r, 80.0) is False


def test_rule_matches_none_value_never_matches():
    r = Rule(">", 85.0, 3)
    assert rule_matches(r, None) is False


def test_rule_matches_lt_and_gte_lte():
    assert rule_matches(Rule("<", 10.0, 1), 5.0) is True
    assert rule_matches(Rule(">=", 10.0, 1), 10.0) is True
    assert rule_matches(Rule("<=", 10.0, 1), 10.0) is True


# --------------------------------------------------------------- step_state --

def test_step_state_immediate_fire_when_n_is_1():
    state, _, transition = step_state("ok", 0, matched=True, n=1)
    assert (state, transition) == ("firing", "breach")


def test_step_state_requires_n_consecutive_matches():
    state, streak = "ok", 0
    # first two matches: still pending, no ping
    state, streak, t1 = step_state(state, streak, True, n=3)
    assert (state, t1) == ("pending", None)
    state, streak, t2 = step_state(state, streak, True, n=3)
    assert (state, t2) == ("pending", None)
    # third consecutive match: fires exactly here
    state, streak, t3 = step_state(state, streak, True, n=3)
    assert (state, t3) == ("firing", "breach")


def test_step_state_no_repeat_ping_while_firing():
    state, streak = "firing", 5
    state, streak, transition = step_state(state, streak, matched=True, n=3)
    assert (state, transition) == ("firing", None)


def test_step_state_recovery_fires_once_leaving_firing():
    state, streak, transition = step_state("firing", 5, matched=False, n=3)
    assert (state, streak, transition) == ("ok", 0, "recovery")


def test_step_state_no_repeat_recovery_once_ok():
    state, _, transition = step_state("ok", 0, matched=False, n=3)
    assert (state, transition) == ("ok", None)


def test_step_state_pending_resets_to_ok_on_non_match():
    state, streak = "pending", 2
    state, streak, transition = step_state(state, streak, matched=False, n=3)
    assert (state, streak, transition) == ("ok", 0, None)


# ------------------------------------------------------------------- notify --

def test_notify_posts_json_text(monkeypatch):
    calls = []

    def fake_post(url, payload):
        calls.append((url, payload))

    notify("https://hooks.slack.com/fake", "hello world", http_post=fake_post)
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://hooks.slack.com/fake"
    assert b"hello world" in payload


def test_notify_swallows_failure_and_does_not_raise(capsys):
    def failing_post(url, payload):
        raise ConnectionError("boom")

    notify("https://hooks.slack.com/fake", "hello", http_post=failing_post)
    err = capsys.readouterr().err
    assert "notify failed" in err


# -------------------------------------------------------- sample_watch e2e --

def test_sample_watch_breach_and_recovery_ping_exactly_once(monkeypatch, tmp_path):
    import numwatch

    posts: list[str] = []
    monkeypatch.setattr(numwatch, "notify", lambda url, text: posts.append(text))
    monkeypatch.setenv("NUMWATCH_SLACK_WEBHOOK", "https://hooks.slack.com/fake")
    values = iter([90, 90, 90, 90, 90, 10, 10])
    monkeypatch.setattr(numwatch, "run_shell", lambda cmd, timeout=30: float(next(values)))

    conn = numwatch.init_db(tmp_path / "t.db")
    w = numwatch.Watch("w", "shell", "x", 1, numwatch.parse_rule("> 85 for 3"), None, "")
    for ts in range(7):
        numwatch.sample_watch(conn, w, {}, ts)

    assert len(posts) == 2
    assert "BREACH" in posts[0] and "RECOVERED" in posts[1]

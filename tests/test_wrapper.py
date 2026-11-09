import json

import pytest

from foundry import ModelWrapper, Paused, RateLimited


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, primary, fallback, clock):
    return ModelWrapper(
        primary=primary,
        fallback=fallback,
        state_path=tmp_path / "state.json",
        log_path=tmp_path / "log.jsonl",
        probe_interval_seconds=60,
        default_cooldown_seconds=300,
        clock=clock,
    )


def test_primary_used_by_default(tmp_path):
    w = make(tmp_path, lambda p: "claude says hi", lambda p: "flash", Clock())
    r = w.call("hello")
    assert (r.text, r.tier, r.label) == ("claude says hi", "primary", "")


def test_fake_429_falls_back_for_bounded_task(tmp_path):
    def limited(prompt):
        raise RateLimited()

    w = make(tmp_path, limited, lambda p: "flash result", Clock())
    r = w.call("write tests", bounded=True)
    assert r.tier == "fallback"
    assert r.label == "[flash]"
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["tier"] == "fallback"
    assert state["resume_at"] == 1300.0  # now + default cooldown


def test_unbounded_task_pauses_instead_of_using_fallback(tmp_path):
    def limited(prompt):
        raise RateLimited(retry_after_seconds=120)

    w = make(tmp_path, limited, lambda p: "flash result", Clock())
    with pytest.raises(Paused) as info:
        w.call("big refactor", bounded=False)
    assert info.value.resume_at == 1120.0


def test_fallback_also_limited_pauses(tmp_path):
    def limited(prompt):
        raise RateLimited()

    w = make(tmp_path, limited, limited, Clock())
    with pytest.raises(Paused):
        w.call("write docs", bounded=True)


def test_returns_to_primary_after_resume_at_with_probe(tmp_path):
    clock = Clock()
    calls = {"n": 0}

    def flaky(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimited(retry_after_seconds=100)
        return "claude is back"

    w = make(tmp_path, flaky, lambda p: "flash result", clock)
    assert w.call("task", bounded=True).tier == "fallback"

    clock.t = 1050.0  # before resume_at: stay on fallback, no probe
    assert w.call("task", bounded=True).tier == "fallback"
    assert calls["n"] == 1

    clock.t = 1200.0  # after resume_at: probe succeeds, back on primary
    r = w.call("task", bounded=True)
    assert r.tier == "primary"
    assert r.text == "claude is back"


def test_failed_probe_stays_on_fallback(tmp_path):
    clock = Clock()

    def limited(prompt):
        raise RateLimited(retry_after_seconds=100)

    w = make(tmp_path, limited, lambda p: "flash result", clock)
    w.call("task", bounded=True)
    clock.t = 1200.0
    r = w.call("task", bounded=True)
    assert r.tier == "fallback"


def test_every_call_is_logged_with_its_tier(tmp_path):
    def limited(prompt):
        raise RateLimited()

    w = make(tmp_path, limited, lambda p: "x", Clock())
    w.call("task", bounded=True)
    events = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert {"rate_limited", "call"} <= {e["event"] for e in events}
    assert any(e.get("tier") == "fallback" for e in events)

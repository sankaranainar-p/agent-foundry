import io
import json
import subprocess
import urllib.error
from email.message import Message
from types import SimpleNamespace

import pytest

from foundry.adapters import ProviderError, agy_cli, build_providers, claude_cli, gemini_api, parse_retry_after
from foundry.wrapper import RateLimited

CLAUDE_PATS = ["hit your limit", "usage limit"]
AGY_PATS = ["RESOURCE_EXHAUSTED", "quota"]


def proc(stdout="", rc=0, stderr=""):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def timeout_run(*a, **k):
    raise subprocess.TimeoutExpired("x", 1)


def cj(**kw):
    return json.dumps({"is_error": False, "subtype": "success", "api_error_status": None, "result": "ok", **kw})


# claude


def test_claude_success():
    assert claude_cli("hi", CLAUDE_PATS, run=proc(cj())) == "ok"


def test_claude_success_text_mentioning_limit_is_not_rate_limited():
    assert claude_cli("hi", CLAUDE_PATS, run=proc(cj(result="usage limit docs"))) == "usage limit docs"


def test_claude_429_with_retry_time():
    out = cj(is_error=True, api_error_status=429, result="Rate limited, try again in 5 minutes")
    with pytest.raises(RateLimited) as e:
        claude_cli("hi", CLAUDE_PATS, run=proc(out, rc=1))
    assert e.value.retry_after_seconds == 300


def test_claude_pattern_without_retry_time():
    out = cj(is_error=True, result="You've hit your limit")
    with pytest.raises(RateLimited) as e:
        claude_cli("hi", CLAUDE_PATS, run=proc(out, rc=1))
    assert e.value.retry_after_seconds is None


def test_claude_unknown_error():
    with pytest.raises(ProviderError):
        claude_cli("hi", CLAUDE_PATS, run=proc(cj(is_error=True, result="boom"), rc=1))


def test_claude_auth_error_is_not_rate_limit():
    out = cj(is_error=True, api_error_status=401, result="Invalid API key")
    with pytest.raises(ProviderError):
        claude_cli("hi", CLAUDE_PATS, run=proc(out, rc=1))


def test_claude_nonzero_exit_garbage_and_timeout():
    with pytest.raises(ProviderError):
        claude_cli("hi", CLAUDE_PATS, run=proc("not json", rc=2, stderr="crash"))
    with pytest.raises(ProviderError):
        claude_cli("hi", CLAUDE_PATS, run=timeout_run)


# agy


def aj(status="SUCCESS", response="ok\n"):
    return json.dumps({"status": status, "response": response})


def test_agy_success_and_args():
    seen = {}

    def run(cmd, **k):
        seen.update(cmd=cmd, cwd=k["cwd"])
        return SimpleNamespace(stdout=aj(), stderr="", returncode=0)

    assert agy_cli("hi", "gemini-x", AGY_PATS, run=run) == "ok\n"
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "gemini-x"
    assert "plan" in seen["cmd"] and seen["cwd"] != "."


def test_agy_rate_limit_with_and_without_retry():
    with pytest.raises(RateLimited) as e:
        agy_cli("hi", "m", AGY_PATS, run=proc("", rc=1, stderr="RESOURCE_EXHAUSTED: retry in 30s"))
    assert e.value.retry_after_seconds == 30
    with pytest.raises(RateLimited) as e:
        agy_cli("hi", "m", AGY_PATS, run=proc(aj("ERROR", "Quota exceeded")))
    assert e.value.retry_after_seconds is None


def test_agy_unknown_auth_exit_and_timeout():
    for p in (proc("", rc=1, stderr="error: not logged in"), proc(aj("ERROR", "boom")), proc("plain text", rc=0)):
        with pytest.raises(ProviderError):
            agy_cli("hi", "m", AGY_PATS, run=p)
    with pytest.raises(ProviderError):
        agy_cli("hi", "m", AGY_PATS, run=timeout_run)


# gemini api


def ok_open(req, timeout):
    body = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    return _Ctx(body)


class _Ctx(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_err(code, body="", headers=None):
    h = Message()
    for k, v in (headers or {}).items():
        h[k] = v
    return lambda req, timeout: (_ for _ in ()).throw(
        urllib.error.HTTPError("https://x", code, "err", h, io.BytesIO(body.encode()))
    )


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY")


def test_gemini_success(key):
    assert gemini_api("hi", "m", urlopen=ok_open) == "hello"


def test_gemini_no_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        gemini_api("hi", "m", urlopen=ok_open)


def test_gemini_429_retry_after_header_and_body_and_none(key):
    with pytest.raises(RateLimited) as e:
        gemini_api("hi", "m", urlopen=http_err(429, headers={"Retry-After": "17"}))
    assert e.value.retry_after_seconds == 17
    body = json.dumps({"error": {"details": [{"retryDelay": "34.5s"}]}})
    with pytest.raises(RateLimited) as e:
        gemini_api("hi", "m", urlopen=http_err(429, body))
    assert e.value.retry_after_seconds == 34.5
    with pytest.raises(RateLimited) as e:
        gemini_api("hi", "m", urlopen=http_err(429, "{}"))
    assert e.value.retry_after_seconds is None


def test_gemini_auth_unknown_timeout_and_no_key_leak(key):
    for code in (400, 403, 500):
        with pytest.raises(ProviderError, match=str(code)) as e:
            gemini_api("hi", "m", urlopen=http_err(code, "bad"))
        assert "SECRET-KEY" not in str(e.value)

    def boom(req, timeout):
        raise TimeoutError("SECRET-KEY")

    with pytest.raises(ProviderError) as e:
        gemini_api("hi", "m", urlopen=boom)
    assert "SECRET-KEY" not in str(e.value)


# wiring and retry parsing


def test_build_providers_gemini_api_needs_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = {"models": {"fallback": "m", "fallback_provider": "gemini-api"}}
    with pytest.raises(ProviderError):
        build_providers(cfg)
    with pytest.raises(ProviderError):
        build_providers({"models": {"fallback": "m", "fallback_provider": "nope"}})
    assert build_providers({"models": {"fallback": "m"}})[2] == "m"  # default agy


def test_parse_retry_after_variants():
    import datetime as dt

    now = dt.datetime(2026, 1, 1, 10, 0, tzinfo=dt.timezone.utc)
    assert parse_retry_after("limit reached|%d" % (now.timestamp() + 90), now) == 90
    assert parse_retry_after("resets 3pm (UTC)", now) == 5 * 3600
    assert parse_retry_after("resets 9am (UTC)", now) == 23 * 3600
    assert parse_retry_after("try again in 2 hours", now) == 7200
    assert parse_retry_after("nothing here", now) is None

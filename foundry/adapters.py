"""Real provider adapters. Each is a callable (prompt: str) -> str, as ModelWrapper expects.

RateLimited means "switch tier". ProviderError means "something else broke": it must
propagate and never trigger the fallback. Keys come from environment variables only and are
never logged or placed in error messages.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml

from .wrapper import Provider, RateLimited

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_UNITS = {"s": 1, "m": 60, "h": 3600}


class ProviderError(Exception):
    """Auth failure, bad output, nonzero exit, timeout, or any non rate limit error."""


# helpers


def _matches(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def parse_retry_after(text: str, now: Optional[dt.datetime] = None) -> Optional[float]:
    """Best effort seconds-until-reset from text. None when there is no hint."""
    now = now or dt.datetime.now().astimezone()
    if m := re.search(r"\|(\d{10})\b", text):  # "usage limit reached|<epoch>"
        return max(0.0, int(m[1]) - now.timestamp())
    if m := re.search(r"(?:in|after)\s+(\d+(?:\.\d+)?)\s*(h|m|s)\w*", text, re.I):
        return float(m[1]) * _UNITS[m[2].lower()]
    if m := re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:\(([\w/+-]+)\))?", text, re.I):
        hour = int(m[1]) % 12 + (12 if m[3].lower() == "pm" else 0)
        tz = now.tzinfo
        if m[4]:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(m[4])
            except Exception:
                pass
        local = now.astimezone(tz)
        target = local.replace(hour=hour, minute=int(m[2] or 0), second=0, microsecond=0)
        if target <= local:
            target += dt.timedelta(days=1)
        return (target - local).total_seconds()
    return None


def _run(run: Callable, cmd: list[str], timeout: float, cwd: str | None = None):
    try:
        return run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise ProviderError(f"{cmd[0]} timed out after {timeout}s") from None
    except OSError as e:
        raise ProviderError(f"cannot run {cmd[0]}: {e}") from None


# claude


def claude_cli(prompt: str, patterns: Iterable[str] = (), timeout: float = 600, run: Callable = subprocess.run) -> str:
    proc = _run(run, ["claude", "-p", prompt, "--output-format", "json"], timeout)
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        raise ProviderError(f"claude exit {proc.returncode}, unparseable output: {(proc.stderr or proc.stdout)[:200]}")
    result = str(data.get("result") or "")
    # Pattern match only on failures, so a normal answer that mentions limits is not misread.
    if data.get("is_error") or proc.returncode != 0:
        if data.get("api_error_status") == 429 or _matches(result, patterns):
            raise RateLimited(parse_retry_after(result))
        raise ProviderError(f"claude error (status {data.get('api_error_status')}): {result[:200]}")
    return result


# agy (Gemini CLI fallback)


def agy_cli(prompt: str, model: str, patterns: Iterable[str] = (), timeout: float = 600, run: Callable = subprocess.run) -> str:
    # --mode plan: no edits. Temp cwd: agy sees none of the repo. Both limit damage if it tries tools.
    cmd = ["agy", "-p", prompt, "--model", model, "--mode", "plan", "--output-format", "json", "--print-timeout", f"{int(timeout)}s"]
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run(run, cmd, timeout + 10, cwd=tmp)
    raw = (proc.stdout or "") + (proc.stderr or "")
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and data.get("status") == "SUCCESS" and proc.returncode == 0:
        return str(data.get("response") or "")
    if _matches(raw, patterns):
        raise RateLimited(parse_retry_after(raw))
    raise ProviderError(f"agy exit {proc.returncode}: {raw[:200]}")


# Gemini REST


def _retry_delay(headers, body: str) -> Optional[float]:
    if (v := headers.get("Retry-After")) and re.fullmatch(r"\d+(\.\d+)?", v.strip()):
        return float(v)
    try:
        for d in json.loads(body)["error"]["details"]:
            if m := re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay", ""))):
                return float(m[1])
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def gemini_api(prompt: str, model: str, timeout: float = 120, urlopen: Callable = urllib.request.urlopen) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ProviderError("GEMINI_API_KEY is not set")
    req = urllib.request.Request(
        GEMINI_URL.format(model=model),
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace") if e.fp else ""
        if e.code == 429:
            raise RateLimited(_retry_delay(e.headers, text)) from None
        raise ProviderError(f"gemini HTTP {e.code}: {text[:200]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ProviderError(f"gemini request failed: {type(e).__name__}") from None
    try:
        return body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(f"gemini returned no text: {str(body.get('promptFeedback', ''))[:200]}") from None


# wiring from project.yaml


def load_config(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def build_providers(cfg: dict) -> tuple[Provider, Provider, str]:
    """Return (primary, fallback, fallback_name) from project.yaml contents."""
    models = cfg.get("models", {})
    pats = cfg.get("rate_limit_patterns", {})
    model = models["fallback"]
    kind = models.get("fallback_provider", "agy")
    primary = lambda p: claude_cli(p, pats.get("claude", []))  # noqa: E731
    if kind == "agy":
        return primary, lambda p: agy_cli(p, model, pats.get("agy", [])), model
    if kind == "gemini-api":
        if not os.environ.get("GEMINI_API_KEY"):
            raise ProviderError("fallback_provider is gemini-api but GEMINI_API_KEY is not set")
        return primary, lambda p: gemini_api(p, model), model
    raise ProviderError(f"unknown fallback_provider: {kind!r} (use agy or gemini-api)")

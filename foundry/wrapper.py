"""Three tier model wrapper.

Tier 1: primary model (Claude) for everything.
Tier 2: fallback model (Gemini Flash), bounded tasks only, only after tier 1 is rate limited.
Tier 3: pause. State is saved with resume_at. Later calls probe tier 1 and resume.

Providers are plain callables: provider(prompt) -> str. They raise RateLimited on a limit error.
Real Claude and Gemini adapters are not included. Pass your own callables.
State lives in a small JSON file, so any process can resume after a restart.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

Provider = Callable[[str], str]

TIER_PRIMARY = "primary"
TIER_FALLBACK = "fallback"


class RateLimited(Exception):
    """Raised by a provider on a rate limit error."""

    def __init__(self, retry_after_seconds: Optional[float] = None):
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class Paused(Exception):
    """Raised when no allowed tier is available. The orchestrator should sleep until resume_at."""

    def __init__(self, resume_at: float):
        super().__init__(f"paused until {resume_at}")
        self.resume_at = resume_at


@dataclass
class CallResult:
    text: str
    tier: str
    model: str
    label: str  # "" for primary, "[flash]" style label for fallback, used in commit messages


class ModelWrapper:
    def __init__(
        self,
        primary: Provider,
        fallback: Provider,
        state_path: str | Path,
        log_path: str | Path | None = None,
        primary_name: str = "claude",
        fallback_name: str = "gemini-flash",
        fallback_label: str = "[flash]",
        probe_interval_seconds: float = 20 * 60,
        default_cooldown_seconds: float = 5 * 60 * 60,
        clock: Callable[[], float] = time.time,
    ):
        self.primary = primary
        self.fallback = fallback
        self.state_path = Path(state_path)
        self.log_path = Path(log_path) if log_path else None
        self.primary_name = primary_name
        self.fallback_name = fallback_name
        self.fallback_label = fallback_label
        self.probe_interval = probe_interval_seconds
        self.default_cooldown = default_cooldown_seconds
        self.clock = clock

    # state

    def _load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"tier": TIER_PRIMARY, "resume_at": 0.0, "last_probe": 0.0, "flash_commits": 0}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2))

    def _log(self, event: dict) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        event["ts"] = self.clock()
        with self.log_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    # calls

    def call(self, prompt: str, bounded: bool = False) -> CallResult:
        """Call a model. bounded=True means the task is allowed to run on the fallback model."""
        state = self._load()
        now = self.clock()

        if state["tier"] == TIER_FALLBACK:
            self._maybe_return_to_primary(state, now)

        if state["tier"] == TIER_PRIMARY:
            try:
                text = self.primary(prompt)
            except RateLimited as err:
                wait = err.retry_after_seconds or self.default_cooldown
                state.update(tier=TIER_FALLBACK, resume_at=now + wait, last_probe=now)
                self._save(state)
                self._log({"event": "rate_limited", "tier": TIER_PRIMARY, "resume_at": state["resume_at"]})
                return self._call_fallback(prompt, bounded, state)
            self._save(state)
            self._log({"event": "call", "tier": TIER_PRIMARY, "model": self.primary_name})
            return CallResult(text, TIER_PRIMARY, self.primary_name, "")

        return self._call_fallback(prompt, bounded, state)

    def _maybe_return_to_primary(self, state: dict, now: float) -> None:
        """Switch back only after resume_at, and only with a successful probe."""
        if now < state["resume_at"]:
            return
        if now - state["last_probe"] < self.probe_interval and state["last_probe"] != 0.0:
            return
        state["last_probe"] = now
        try:
            self.primary("ping")
        except RateLimited as err:
            wait = err.retry_after_seconds or self.probe_interval
            state["resume_at"] = now + wait
            self._save(state)
            self._log({"event": "probe_failed", "resume_at": state["resume_at"]})
            return
        state["tier"] = TIER_PRIMARY
        self._save(state)
        self._log({"event": "returned_to_primary"})

    def _call_fallback(self, prompt: str, bounded: bool, state: dict) -> CallResult:
        if not bounded:
            # Unbounded work waits for the primary model. It never runs on the fallback.
            self._log({"event": "paused", "reason": "unbounded task, primary limited"})
            raise Paused(state["resume_at"])
        try:
            text = self.fallback(prompt)
        except RateLimited as err:
            wait = err.retry_after_seconds or self.probe_interval
            state["resume_at"] = max(state["resume_at"], self.clock() + wait)
            self._save(state)
            self._log({"event": "paused", "reason": "fallback limited", "resume_at": state["resume_at"]})
            raise Paused(state["resume_at"])
        state["flash_commits"] = state.get("flash_commits", 0) + 1
        self._save(state)
        self._log({"event": "call", "tier": TIER_FALLBACK, "model": self.fallback_name})
        return CallResult(text, TIER_FALLBACK, self.fallback_name, self.fallback_label)

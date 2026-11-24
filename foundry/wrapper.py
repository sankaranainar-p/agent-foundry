"""Three tier model wrapper.

Tier 1: primary model (Claude) for everything.
Tier 2: fallback model (Gemini Flash), bounded tasks only, only after tier 1 is rate limited.
Tier 3: pause. State is saved with resume_at. Later calls probe tier 1 and resume.

Providers are plain callables: provider(prompt) -> str. They raise RateLimited on a limit error.
Real adapters live in foundry/adapters.py (claude_cli, agy_cli, gemini_api). Providers raise
RateLimited only for limits; any other failure raises adapters.ProviderError, which this wrapper
does not catch, so it never triggers the fallback.
Tiers change only between tasks: call begin_task() at the start of each task and every call
until the next begin_task() stays on that tier. A limit hit mid-task on the primary raises
TierChanged (state saved, restart the task); on the fallback it raises Paused. Without
begin_task() the wrapper switches per call.
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


class TierChanged(Exception):
    """Primary hit a limit in the middle of a task. The tier switch is saved and takes effect at
    the next begin_task(), so the caller restarts the task."""

    def __init__(self, resume_at: float, reason: str = ""):
        super().__init__(f"{reason}, resume_at={resume_at}")
        self.resume_at = resume_at
        self.reason = reason


class Paused(Exception):
    """Raised when no allowed tier is available. The orchestrator should sleep until resume_at."""

    def __init__(self, resume_at: float, reason: str = ""):
        super().__init__(f"paused until {resume_at}" + (f" ({reason})" if reason else ""))
        self.resume_at = resume_at
        self.reason = reason


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
        self._pinned: Optional[str] = None  # tier fixed by begin_task(), None outside a task

    # state

    def _load(self) -> dict:
        state = {"tier": TIER_PRIMARY, "resume_at": 0.0, "last_probe": 0.0, "flash_commits": 0,
                 "current_task": "", "paused_task": ""}
        if self.state_path.exists():
            state.update(json.loads(self.state_path.read_text()))
        return state

    def snapshot(self) -> dict:
        return self._load()

    def set_meta(self, **kv) -> None:
        """Persist extra facts (for example paused_task) next to the tier state."""
        state = self._load()
        state.update(kv)
        self._save(state)

    def begin_task(self, task_id: str = "") -> None:
        """Tier changes happen only here. Probe for a return to primary, then keep that tier
        for every call until the next begin_task()."""
        state = self._load()
        if state["tier"] == TIER_FALLBACK:
            self._maybe_return_to_primary(state, self.clock())
        state["current_task"] = task_id
        self._save(state)
        self._pinned = state["tier"]

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

    @property
    def active_tier(self) -> str:
        """The tier the next call will try first: the pinned tier inside a task, else the saved one."""
        return self._pinned or self._load()["tier"]

    def call(self, prompt: str, bounded: bool = False, primary: Optional[Provider] = None,
             role: str = "", sections: Optional[dict[str, int]] = None) -> CallResult:
        """Call a model. bounded=True means the task is allowed to run on the fallback model.
        primary overrides the primary provider for this call only (for example a tool-using agent
        run in a worktree); rate limits and tiers behave exactly as for the default primary."""
        state = self._load()
        now = self.clock()

        pinned = self._pinned
        if pinned is None and state["tier"] == TIER_FALLBACK:
            self._maybe_return_to_primary(state, now)

        if (pinned or state["tier"]) == TIER_PRIMARY:
            started = time.monotonic()
            try:
                text = (primary or self.primary)(prompt)
            except RateLimited as err:
                wait = err.retry_after_seconds or self.default_cooldown
                state.update(tier=TIER_FALLBACK, resume_at=now + wait, last_probe=now)
                self._save(state)
                self._log({"event": "rate_limited", "tier": TIER_PRIMARY, "resume_at": state["resume_at"]})
                if pinned:
                    raise TierChanged(state["resume_at"], "primary rate limited mid-task")
                return self._call_fallback(prompt, bounded, state, role, sections)
            self._log(self._usage(state, text, prompt, started, role, TIER_PRIMARY, self.primary_name, sections))
            self._save(state)
            return CallResult(str(text), TIER_PRIMARY, self.primary_name, "")

        return self._call_fallback(prompt, bounded, state, role, sections)

    def _usage(self, state: dict, text: str, prompt: str, started: float, role: str, tier: str, model: str,
               sections: Optional[dict[str, int]] = None) -> dict:
        """One call event: role, sizes, duration, and cost and tokens when the adapter reports them
        (claude_cli returns a Reply with .meta). Cost also accumulates in state["cost_usd"]."""
        meta = getattr(text, "meta", None) or {}
        if meta.get("cost_usd"):
            state["cost_usd"] = round(state.get("cost_usd", 0.0) + float(meta["cost_usd"]), 6)
        event = {"event": "call", "tier": tier, "model": model, "role": role,
                 "duration_s": round(time.monotonic() - started, 3),
                 "prompt_chars": len(prompt), "reply_chars": len(text)}
        if sections:
            event["section_chars"] = sections
        event.update({k: v for k, v in meta.items() if v is not None})
        return event

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

    def _call_fallback(self, prompt: str, bounded: bool, state: dict, role: str = "",
                       sections: Optional[dict[str, int]] = None) -> CallResult:
        if not bounded:
            # Unbounded work waits for the primary model. It never runs on the fallback.
            self._log({"event": "paused", "reason": "unbounded task, primary limited"})
            raise Paused(state["resume_at"], "unbounded task, primary limited")
        started = time.monotonic()
        try:
            text = self.fallback(prompt)
        except RateLimited as err:
            wait = err.retry_after_seconds or self.probe_interval
            state["resume_at"] = max(state["resume_at"], self.clock() + wait)
            self._save(state)
            self._log({"event": "paused", "reason": "fallback limited", "resume_at": state["resume_at"]})
            raise Paused(state["resume_at"], "fallback limited")
        state["flash_commits"] = state.get("flash_commits", 0) + 1
        self._log(self._usage(state, text, prompt, started, role, TIER_FALLBACK, self.fallback_name, sections))
        self._save(state)
        return CallResult(str(text), TIER_FALLBACK, self.fallback_name, self.fallback_label)

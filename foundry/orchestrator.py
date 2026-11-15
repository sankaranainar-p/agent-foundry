"""Orchestrator loop.

Agents are callables supplied by you, so this loop is model and tool agnostic:

    developer(task, notes) -> str          returns a branch name or summary
    verifier(task, work) -> (bool, str)    optional: build and test commands
    reviewer(task, work) -> (bool, str)    approve flag and notes
    tester(task, work) -> (bool, str)      pass flag and evidence path or notes
    merger(task, work) -> (bool, str)      optional: merge into the integration branch

A failure at any stage routes back to the developer with the notes, up to review_retry_cap times.
A task that exhausts its retries is marked blocked. The queue stops there (later tasks may depend
on it) unless keep_going is set. The human sees blocked tasks in the status report.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .tasks import Task, in_milestone, next_open_task, parse_tasks, set_status
from .wrapper import Paused, TierChanged


class AgentFailed(Exception):
    """An agent produced unusable output (no diff, patch does not apply). Routes back to the
    developer as notes, like a review failure."""

Developer = Callable[[Task, str], str]
Reviewer = Callable[[Task, str], tuple[bool, str]]
Tester = Callable[[Task, str], tuple[bool, str]]


@dataclass
class Outcome:
    task_id: str
    status: str  # done | blocked | paused | restart (tier changed mid-task, rerun it)
    attempts: int
    notes: str = ""


def _ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def run_task(
    task: Task,
    developer: Developer,
    reviewer: Reviewer,
    tester: Tester,
    retry_cap: int = 3,
    merger: Optional[Callable[[Task, str], tuple[bool, str]]] = None,
    on_attempt: Optional[Callable[[Task, int], None]] = None,
    emit: Callable[[str], None] = lambda msg: None,
    record: Callable[[str, str], None] = lambda name, text: None,
    verifier: Optional[Tester] = None,
    precheck: Optional[Callable[[Task], str]] = None,
) -> Outcome:
    """on_attempt(task, n) runs at the start of each attempt, emit(msg) reports progress, and
    record(name, text) saves a file for the current attempt (notes.txt holds the attempt's
    end notes, so a blocked task's reason is its last attempt's notes.txt).
    verifier(task, work) runs the project's build and test commands before the review, so the
    reviewer never spends a call on code that does not build or pass its tests."""
    if precheck and (problem := precheck(task)):  # config error: no developer call can fix it
        if on_attempt:
            on_attempt(task, 1)  # sets the log dir for record()
        problem = f"config: {problem}"
        emit(f"  {problem}")
        record("notes.txt", problem)
        return Outcome(task.id, "blocked", 0, problem)
    notes = ""
    for attempt in range(1, retry_cap + 1):
        if on_attempt:
            on_attempt(task, attempt)
        stage = "developer"
        try:
            work = developer(task, notes)
            emit(f"  developer ok ({work})")
            if verifier:
                stage = "verify"
                passed, notes = verifier(task, work)
                emit(f"  build/test: {'pass' if passed else 'fail'}")
                if not passed:
                    record("notes.txt", notes)
                    continue
            stage = "reviewer"
            approved, notes = reviewer(task, work)
            if not approved:
                emit(f"  reviewer: not approved ({notes.strip().splitlines()[0][:100] if notes.strip() else 'empty reply'})")
                record("notes.txt", notes)
                continue
            emit("  reviewer: approved")
            stage = "smoke"
            passed, notes = tester(task, work)
            emit(f"  smoke: {'pass' if passed else 'fail'}")
            if not passed:
                record("notes.txt", notes)
                continue
            if merger:  # the orchestrator merges, and only after review and tests pass
                stage = "merge"
                merged, merge_notes = merger(task, work)
                if not merged:
                    record("notes.txt", merge_notes)
                    return Outcome(task.id, "blocked", attempt, merge_notes)
                emit("  merged into integration branch")
            return Outcome(task.id, "done", attempt)
        except AgentFailed as e:
            notes = str(e)
            emit(f"  {stage} failed: {notes}")
            record("failure.txt", notes)
            record("notes.txt", notes)
        except TierChanged as e:
            return Outcome(task.id, "restart", attempt, f"{e.reason}; resume_at={_ts(e.resume_at)}")
        except Paused as p:
            return Outcome(task.id, "paused", attempt, f"{p.reason or 'rate limited'}; resume_at={_ts(p.resume_at)}")
    return Outcome(task.id, "blocked", retry_cap, notes)


def run_queue(
    tasks_path: str | Path,
    developer: Developer,
    reviewer: Reviewer,
    tester: Tester,
    retry_cap: int = 3,
    begin_task: Optional[Callable[[str], None]] = None,
    after_task: Optional[Callable[[Task, Outcome], None]] = None,
    merger: Optional[Callable[[Task, str], tuple[bool, str]]] = None,
    keep_going: bool = False,
    on_attempt: Optional[Callable[[Task, int], None]] = None,
    emit: Callable[[str], None] = lambda msg: None,
    record: Callable[[str, str], None] = lambda name, text: None,
    verifier: Optional[Tester] = None,
    milestone: Optional[str] = None,
    already_done: Optional[Callable[[Task], bool]] = None,
    precheck: Optional[Callable[[Task], str]] = None,
) -> list[Outcome]:
    """Work open tasks in order. Stops on a pause so the caller can sleep until resume_at, and
    on a blocked task unless keep_going (later tasks may depend on it; they stay open, and with
    keep_going only tasks whose `Depends on` are all done still run).

    begin_task(task_id) runs before each task (wrapper.begin_task: the only place tiers change).
    after_task(task, outcome) runs after each task, including a pause (status report).
    milestone limits the queue to tasks with that `Milestone:`.
    already_done(task) is checked first: True means the work was merged by an earlier run that
    crashed before writing the status, so the task is marked done without rerunning it."""
    path = Path(tasks_path)
    outcomes: list[Outcome] = []
    while True:
        text = path.read_text()
        tasks = parse_tasks(text)
        task = next_open_task(tasks, milestone)
        if task is None:
            waiting = [t.id for t in tasks if t.status == "open" and in_milestone(t, milestone)]
            if waiting:
                emit(f"{len(waiting)} open task(s) wait on unfinished dependencies: {', '.join(waiting)}")
            elif not outcomes:
                emit(f"no open tasks in {path}" + (f" for milestone {milestone}" if milestone else ""))
            return outcomes
        if already_done and already_done(task):
            path.write_text(set_status(text, task.id, "done"))
            emit(f"{task.id} already merged into the integration branch; marked done")
            outcomes.append(Outcome(task.id, "done", 0, "already merged"))
            continue
        if begin_task:
            begin_task(task.id)
        outcome = run_task(task, developer, reviewer, tester, retry_cap, merger, on_attempt, emit, record, verifier, precheck)
        if outcome.status == "restart":
            emit(f"{task.id} tier changed ({outcome.notes}); restarting the task")
            continue  # tier switched; begin_task applies it on the rerun
        outcomes.append(outcome)
        if outcome.status != "paused":
            path.write_text(set_status(text, task.id, outcome.status))
        if after_task:
            after_task(task, outcome)
        if outcome.status == "done":
            emit(f"{task.id} done")
        elif outcome.status == "blocked":
            emit(f"{task.id} blocked: {outcome.notes[:500]}")
        else:
            emit(f"{task.id} paused: {outcome.notes}")
        if outcome.status == "paused" or (outcome.status == "blocked" and not keep_going):
            return outcomes


def run_until_done(
    run_once: Callable[[], list[Outcome]],
    get_resume_at: Callable[[], float],
    probe_interval: float,
    wait: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> list[Outcome]:
    """Run the queue; on a pause sleep until resume_at (re-read every probe interval), then rerun.
    wait=False returns at the first pause. The queue itself is the resume state: paused tasks
    stay open, and the wrapper state file holds the tier and resume_at."""
    all_outcomes: list[Outcome] = []
    while True:
        outcomes = run_once()
        all_outcomes += outcomes
        if not outcomes or outcomes[-1].status != "paused" or not wait:
            return all_outcomes
        slept = False
        while True:
            left = get_resume_at() - clock()
            if left <= 0 and slept:
                break
            sleep(min(left, probe_interval) if left > 0 else probe_interval)
            slept = True


def write_status(path: str | Path, tasks_text: str, state: dict, extra: tuple[str, ...] = ()) -> None:
    """logs/status.md: counts, current task, active tier, resume_at."""
    tasks = parse_tasks(tasks_text)
    count = lambda st: sum(t.status == st for t in tasks)  # noqa: E731
    resume = state.get("resume_at") or 0
    resume_s = _ts(resume) if resume else "none"
    lines = [
        "# Factory status",
        "",
        f"- done: {count('done')}",
        f"- blocked: {count('blocked')}",
        f"- paused: {1 if state.get('paused_task') else 0}" + (f" ({state['paused_task']})" if state.get("paused_task") else ""),
        f"- open: {count('open')}",
        f"- current task: {state.get('current_task') or 'none'}",
        f"- active tier: {state.get('tier')}",
        f"- resume_at: {resume_s}",
        *([f"- cost so far: ${state['cost_usd']:.2f} (as reported by the claude CLI)"] if state.get("cost_usd") else []),
        *[f"- {line}" for line in extra],
        "",
    ]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines))

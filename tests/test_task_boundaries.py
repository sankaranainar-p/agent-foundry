import pytest

from foundry import ModelWrapper, Paused, RateLimited, TierChanged
from foundry.orchestrator import run_queue, run_until_done, write_status
from foundry.tasks import parse_tasks
from test_tasks_and_orchestrator import SAMPLE
from test_wrapper import Clock, make

OK = lambda t, w: (True, "")  # noqa: E731


class Switch:
    """Primary that raises RateLimited while .limited is True."""

    def __init__(self):
        self.limited = False

    def __call__(self, prompt):
        if self.limited:
            raise RateLimited(100)
        return "claude"


# item 3: tier changes only between tasks


def test_primary_limit_mid_task_raises_tier_changed_and_next_task_uses_fallback(tmp_path):
    clock, prim = Clock(), Switch()
    w = make(tmp_path, prim, lambda p: "flash", clock)
    w.begin_task("T-001")
    assert w.call("a", bounded=True).tier == "primary"
    prim.limited = True
    with pytest.raises(TierChanged):
        w.call("b", bounded=True)  # no silent mid-task switch
    w.begin_task("T-001")  # rerun
    assert w.call("c", bounded=True).tier == "fallback"


def test_fallback_task_stays_on_fallback_even_after_resume_time(tmp_path):
    clock, prim = Clock(), Switch()
    w = make(tmp_path, prim, lambda p: "flash", clock)
    prim.limited = True
    w.begin_task("T-001")
    with pytest.raises(TierChanged):
        w.call("x", bounded=True)
    w.begin_task("T-001")
    prim.limited = False
    clock.t += 10_000  # primary is healthy again, but we are mid-task
    assert w.call("y", bounded=True).tier == "fallback"
    with pytest.raises(Paused):  # unbounded work waits for the next boundary
        w.call("z", bounded=False)
    w.begin_task("T-002")  # boundary: probe succeeds, back to primary
    assert w.call("w").tier == "primary"


def test_queue_restarts_task_after_tier_change(tmp_path):
    clock, prim = Clock(), Switch()
    w = make(tmp_path, prim, lambda p: "flash", clock)
    path = tmp_path / "tasks.md"
    path.write_text(SAMPLE)
    tiers = []

    def dev(task, notes):
        if task.id == "T-001" and not prim.limited and not tiers:
            prim.limited = True
        tiers.append((task.id, w.call("p", bounded=True).tier))
        return "b"

    outs = run_queue(path, dev, OK, OK, begin_task=w.begin_task)
    assert [o.status for o in outs] == ["done", "done"]
    assert tiers[0] == ("T-001", "fallback")  # first attempt restarted on fallback


# item 4: sleep and resume


def test_run_until_done_sleeps_until_resume_at_then_continues(tmp_path):
    clock, prim = Clock(), Switch()
    prim.limited = True
    w = make(tmp_path, prim, lambda p: "flash", clock)
    path = tmp_path / "tasks.md"
    path.write_text(SAMPLE)
    slept = []

    def sleep(sec):
        slept.append(sec)
        clock.t += sec
        prim.limited = False  # limit lifts while we sleep

    def dev(task, notes):
        w.call("p", bounded=False)  # unbounded: needs Claude
        return "b"

    run_once = lambda: run_queue(path, dev, OK, OK, begin_task=w.begin_task)  # noqa: E731
    outs = run_until_done(run_once, lambda: w.snapshot()["resume_at"], 60, sleep=sleep, clock=clock)
    assert [o.status for o in outs] == ["paused", "done", "done"]
    assert slept and all(s <= 60 for s in slept) and sum(slept) >= 100


def test_no_wait_exits_at_first_pause_and_state_survives_restart(tmp_path):
    clock, prim = Clock(), Switch()
    prim.limited = True
    w = make(tmp_path, prim, lambda p: "flash", clock)
    path = tmp_path / "tasks.md"
    path.write_text(SAMPLE)

    def dev(task, notes):
        w.call("p", bounded=False)
        return "b"

    outs = run_until_done(lambda: run_queue(path, dev, OK, OK, begin_task=w.begin_task),
                          lambda: 0, 60, wait=False, sleep=lambda s: pytest.fail("slept"), clock=clock)
    assert [o.status for o in outs] == ["paused"]
    w2 = make(tmp_path, Switch(), lambda p: "flash", clock)  # "restart": new process, same state file
    st = w2.snapshot()
    assert (st["tier"], st["resume_at"], st["current_task"]) == ("fallback", clock.t + 100, "T-001")


# item 5: status.md


def test_status_report_written_after_each_task(tmp_path):
    clock = Clock()
    w = make(tmp_path, lambda p: "claude", lambda p: "flash", clock)
    path, status = tmp_path / "tasks.md", tmp_path / "logs" / "status.md"
    path.write_text(SAMPLE)
    seen = []

    def after(task, outcome):
        write_status(status, path.read_text(), w.snapshot())
        seen.append(status.read_text())

    run_queue(path, lambda t, n: "b", lambda t, w_: (t.id == "T-001", "no"), OK, retry_cap=1,
              begin_task=w.begin_task, after_task=after)
    assert len(seen) == 2
    assert "- done: 1" in seen[0] and "- current task: T-001" in seen[0] and "- active tier: primary" in seen[0]
    assert "- blocked: 1" in seen[1] and "- done: 1" in seen[1] and "- paused: 0" in seen[1]
    assert "- resume_at: none" in seen[1]

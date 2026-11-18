"""python -m foundry plan <project_dir> | run <project_dir> [--no-wait] [--keep-going] [--milestone M] [--quiet] | audit <project_dir>

Exit codes for run: 0 all done, 1 a task is blocked or tasks.md is invalid, 2 paused (with --no-wait)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .adapters import build_agent, build_providers, load_config
from .orchestrator import run_queue, run_until_done, write_status
from .runners import Runners
from .tasks import parse_tasks, validate_tasks
from .wrapper import ModelWrapper

PARKING_SPLIT = re.compile(r"(?m)^#\s+Parking\s+Lot\b")


def make_wrapper(project: Path, cfg: dict, primary, fallback, name: str, **kw) -> ModelWrapper:
    lim = cfg.get("limits", {})
    return ModelWrapper(
        primary, fallback, project / "logs" / "state.json", project / "logs" / "calls.jsonl",
        fallback_name=name,
        probe_interval_seconds=lim.get("probe_interval_minutes", 20) * 60,
        default_cooldown_seconds=lim.get("default_cooldown_minutes", 300) * 60,
        **kw,
    )


def _split_audit(text: str) -> tuple[str, str]:
    """Split a gap-analyzer reply into (gap_report, parking_lot) when a `# Parking Lot` heading exists."""
    m = PARKING_SPLIT.search(text)
    if not m:
        return text, ""
    return text[:m.start()].rstrip() + "\n", text[m.start():].lstrip()


def main(argv=None, providers=None, agent_runner=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").add_argument("project")
    r = sub.add_parser("run")
    r.add_argument("project")
    r.add_argument("--no-wait", action="store_true", help="exit at the first pause instead of sleeping")
    r.add_argument("--keep-going", action="store_true", help="continue past blocked tasks (default: stop at the first)")
    r.add_argument("--milestone", default="", help="work only tasks whose Milestone field matches")
    r.add_argument("--quiet", action="store_true", help="no progress output on stderr")
    sub.add_parser("audit").add_argument("project")
    args = ap.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    cfg = load_config(project / "project.yaml")
    primary, fallback, name = providers or build_providers(cfg)
    wrapper = make_wrapper(project, cfg, primary, fallback, name)
    runner_fn = agent_runner if agent_runner is not None else (None if providers else build_agent(cfg))
    runners = Runners(project, wrapper, cfg, agent_runner=runner_fn)

    if args.cmd == "plan":
        out = project / "tasks.proposed.md"
        text = runners.planner()
        reply = project / "logs" / "planner-reply.md"
        reply.parent.mkdir(exist_ok=True)
        reply.write_text(text)
        if not parse_tasks(text):
            print(f"planner reply has no tasks (headings must look like `## T-001: Title`); full reply in {reply}",
                  file=sys.stderr)
            return 1
        out.write_text(text)
        problems = validate_tasks(text, runners.doc_text("spec"))
        if problems:
            print(f"wrote {out}, with validation warnings:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"wrote {out}. Review it, then move it over tasks.md to approve.")
        return 0

    if args.cmd == "audit":
        reply = runners.gap_analyzer()
        gap_text, lot_text = _split_audit(reply)
        gap_path = project / "logs" / "gap-report.md"
        gap_path.parent.mkdir(parents=True, exist_ok=True)
        gap_path.write_text(gap_text)
        if lot_text:
            (project / "logs" / "parking-lot.md").write_text(lot_text)
        print(f"wrote {gap_path}")
        return 0

    emit = (lambda msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
    tasks_path, status_path = project / "tasks.md", project / "logs" / "status.md"
    if problems := validate_tasks(tasks_path.read_text(), runners.doc_text("spec")):
        emit(f"invalid {tasks_path}:")
        for p in problems:
            emit(f"  - {p}")
        return 1

    runners.ensure_integration()
    emit(runners.integration_line())

    def after(task, outcome):
        wrapper.set_meta(paused_task=task.id if outcome.status == "paused" else "")
        write_status(status_path, tasks_path.read_text(), wrapper.snapshot(), (runners.integration_line(),))

    def on_attempt(task, n):
        runners.set_attempt(task.id, n)
        emit(f"{task.id} start (tier: {wrapper.snapshot()['tier']}, attempt {n})")

    run_once = lambda: run_queue(  # noqa: E731
        tasks_path, runners.developer, runners.reviewer, runners.playwright,
        retry_cap=cfg.get("limits", {}).get("review_retry_cap", 3),
        begin_task=wrapper.begin_task, after_task=after, merger=runners.merge,
        keep_going=args.keep_going, on_attempt=on_attempt, emit=emit, record=runners.record,
        verifier=runners.verify, milestone=args.milestone or None, already_done=runners.is_merged,
        precheck=runners.verify_problem,
    )
    outcomes = run_until_done(run_once, lambda: wrapper.snapshot()["resume_at"], wrapper.probe_interval,
                              wait=not args.no_wait)
    count = lambda st: sum(o.status == st for o in outcomes)  # noqa: E731
    emit(f"summary: {count('done')} done, {count('blocked')} blocked, {count('paused')} paused. Status: {status_path}")
    for o in outcomes:
        if o.status == "blocked":
            emit(f"  {o.task_id} blocked after {o.attempts} attempt(s); see {project / 'logs' / o.task_id / f'attempt-{o.attempts}'}")
    if outcomes and outcomes[-1].status == "paused":
        return 2
    return 1 if count("blocked") else 0


if __name__ == "__main__":
    sys.exit(main())

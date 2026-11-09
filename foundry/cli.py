"""python -m foundry plan <project_dir> | run <project_dir> [--no-wait] [--keep-going] [--quiet]

Exit codes for run: 0 all done, 1 a task is blocked, 2 paused (with --no-wait)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapters import build_providers, load_config
from .orchestrator import run_queue, run_until_done, write_status
from .runners import Runners
from .wrapper import ModelWrapper


def make_wrapper(project: Path, cfg: dict, primary, fallback, name: str, **kw) -> ModelWrapper:
    lim = cfg.get("limits", {})
    return ModelWrapper(
        primary, fallback, project / "logs" / "state.json", project / "logs" / "calls.jsonl",
        fallback_name=name,
        probe_interval_seconds=lim.get("probe_interval_minutes", 20) * 60,
        default_cooldown_seconds=lim.get("default_cooldown_minutes", 300) * 60,
        **kw,
    )


def main(argv=None, providers=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan").add_argument("project")
    r = sub.add_parser("run")
    r.add_argument("project")
    r.add_argument("--no-wait", action="store_true", help="exit at the first pause instead of sleeping")
    r.add_argument("--keep-going", action="store_true", help="continue past blocked tasks (default: stop at the first)")
    r.add_argument("--quiet", action="store_true", help="no progress output on stderr")
    args = ap.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    cfg = load_config(project / "project.yaml")
    primary, fallback, name = providers or build_providers(cfg)
    wrapper = make_wrapper(project, cfg, primary, fallback, name)
    runners = Runners(project, wrapper, cfg)

    if args.cmd == "plan":
        out = project / "tasks.proposed.md"
        out.write_text(runners.planner())
        print(f"wrote {out}. Review it, then move it over tasks.md to approve.")
        return 0

    emit = (lambda msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
    runners.ensure_integration()
    emit(runners.integration_line())
    tasks_path, status_path = project / "tasks.md", project / "logs" / "status.md"

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

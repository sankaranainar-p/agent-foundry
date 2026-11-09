"""python -m foundry plan <project_dir> | run <project_dir> [--no-wait]"""

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

    tasks_path, status_path = project / "tasks.md", project / "logs" / "status.md"

    def after(task, outcome):
        wrapper.set_meta(paused_task=task.id if outcome.status == "paused" else "")
        write_status(status_path, tasks_path.read_text(), wrapper.snapshot())

    run_once = lambda: run_queue(  # noqa: E731
        tasks_path, runners.developer, runners.reviewer, runners.playwright,
        retry_cap=cfg.get("limits", {}).get("review_retry_cap", 3),
        begin_task=wrapper.begin_task, after_task=after,
    )
    outcomes = run_until_done(run_once, lambda: wrapper.snapshot()["resume_at"], wrapper.probe_interval,
                              wait=not args.no_wait)
    for o in outcomes:
        print(f"{o.task_id}: {o.status}")
    return 2 if outcomes and outcomes[-1].status == "paused" else 0


if __name__ == "__main__":
    sys.exit(main())

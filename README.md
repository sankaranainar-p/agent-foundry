# agent-foundry

[![tests](https://github.com/sankaranainar-p/agent-foundry/actions/workflows/tests.yml/badge.svg)](https://github.com/sankaranainar-p/agent-foundry/actions/workflows/tests.yml)

A generic, spec-driven software factory for AI agents. A reviewed spec goes in. Verified code comes out.

Design and analysis happen up front with [EGM](https://github.com/google/elephant-goldfish-model) (elephant and goldfish) and, optionally, GitHub Spec Kit. Then a pipeline of agents builds, reviews, tests, and audits the work, and survives rate limits without losing state.

## Pipeline

```
design phase (you + EGM)          execution phase (agent-foundry)
brainstorm > draft > review  ->   Planner > Developer > PR Reviewer > Playwright > Gap Analyzer
spec.md, plan.md approved         tasks.md approved, then loop until milestone is clean
```

| Agent | Job | Role file |
| --- | --- | --- |
| 0. Planner | Reviews the spec, splits it into ordered tasks with requirement IDs and done-checks | `agents/planner.md` |
| 1. Developer | Implements one task on a branch, commits small | `agents/developer.md` |
| 2. PR Reviewer | Reviews the diff in a fresh context | `agents/reviewer.md` |
| 3. Playwright Tester | Runs browser flows (or a smoke test if no UI) and saves evidence | `agents/playwright.md` |
| 4. Gap Analyzer | Audits the milestone against the spec by requirement ID | `agents/gap-analyzer.md` |

## Model tiers

The wrapper in `foundry/wrapper.py` sends every call through three tiers:

1. Claude, for everything.
2. Gemini Flash, only for bounded tasks (tests, docs, small fixes), only after Claude hits a limit.
3. Pause: state is saved, the orchestrator sleeps until `resume_at`, then probes Claude and resumes.

Flash commits are labeled. Claude reviews them first after the switch back. No model grades its own work.

## Use it on a project

```bash
python3 scripts/init_project.py /path/to/your/project
```

This copies the templates into the project and writes a `project.yaml` you fill in (build, test, and run commands, whether it has a UI, allowed models). Then:

1. Run the design phase in Claude Code with EGM: `/egm:egm-brainstorm`, `/egm:egm-draft`, `/egm:egm-review`.
2. Save the approved design as `spec.md` and `plan.md` in the project.
3. Run the Planner to produce `tasks.md`. Approve it.
4. Run the orchestrator.

## Layout

```
agents/       role files, one per agent
templates/    constitution, spec, plan, tasks, project.yaml
foundry/      wrapper (tiers, probe, logging), adapters, runners, cli, tasks parser, orchestrator loop
scripts/      init_project.py, smoke_adapters.py
tests/        wrapper and parser tests, including a fake 429 test
examples/     worked example (spanslo)
```

## Status

Runnable, not yet proven on a real project. Implemented and tested with fake providers: the wrapper, task parser, orchestrator loop, real adapters (`foundry/adapters.py`: `claude_cli`, `agy_cli`, `gemini_api`, picked by `models.fallback_provider`), and one runner per agent (`foundry/runners.py`).

```bash
python -m foundry plan <project_dir>            # writes tasks.proposed.md; you approve by moving it over tasks.md
python -m foundry run <project_dir> [--no-wait] # works the queue; sleeps through rate limits unless --no-wait
```

- Tiers change only between tasks. A limit hit mid-task restarts the task on the new tier; a fallback task never returns to Claude until the next task.
- The developer asks the model for a unified diff, applies it with `git apply`, and commits on a branch named after the task ID. It never merges. Flash commits end with `[flash]`.
- A pause keeps the task open. State is in `logs/state.json`; `logs/status.md` is rewritten after every task.
- Playwright: only `has_ui: false` works (runs `commands.smoke`, saves output under `logs/evidence/<task-id>/`). `has_ui: true` raises `NotImplementedError`.
- Not done: the developer does not run `commands.test` itself, the gap analyzer has no CLI command, and `python scripts/smoke_adapters.py` makes one real call per tier.

## Rules

- API keys come from environment variables only. Never commit them.
- Agents never add scope. Work with no requirement ID goes to a parking lot for you to decide.
- EGM and Spec Kit are dependencies. Install them, do not copy their files here.

## License

Apache License 2.0. See `LICENSE`.

# agent-foundry

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.24579451.svg)](https://doi.org/10.5281/zenodo.24579451)
[![tests](https://github.com/sankaranainar-p/agent-foundry/actions/workflows/tests.yml/badge.svg)](https://github.com/sankaranainar-p/agent-foundry/actions/workflows/tests.yml)

A generic, spec-driven software factory for AI agents. A reviewed spec goes in. Verified code comes out.

**Status: v0.1.1, early.** It built the first release of [spanslo](https://github.com/sankaranainar-p/spanslo) end to end. Interfaces can change before 1.0. See [Limits](#limits).

Design and analysis happen up front with [EGM](https://github.com/google/elephant-goldfish-model) (elephant and goldfish) and, optionally, GitHub Spec Kit. Then a pipeline of agents builds, verifies, reviews, tests, and audits the work inside isolated git worktrees, and survives rate limits without losing state.

## Pipeline

```
design phase (you + EGM)          execution phase (agent-foundry)
brainstorm > draft > review  ->   Planner > Developer > Verify (build/test) > PR Reviewer > Smoke/Playwright > Gap Analyzer
spec.md, plan.md approved         tasks.md approved, then loop until milestone is clean
```

| Stage | Job | Role file (override in `<project>/agents/`) |
| --- | --- | --- |
| 0. Planner | Reviews the spec, splits it into ordered tasks with requirement IDs, dependencies, milestones, and done-checks | `foundry/agents/planner.md` |
| 1. Developer | Implements one task in an isolated git worktree (`mode: agent` edits files with tools; `mode: diff` applies a unified diff) | `foundry/agents/developer.md` |
| 1b. Verify | Deterministic gate: runs `commands.build` and `commands.test` in the worktree before calling the reviewer | `foundry/runners.py` |
| 2. PR Reviewer | Reviews the diff + full touched files in a fresh context | `foundry/agents/reviewer.md` |
| 3. Playwright / Smoke | Runs `commands.smoke` (or browser flows when `has_ui: true`) and saves evidence | `foundry/agents/playwright.md` |
| 4. Gap Analyzer | Audits the milestone against the spec by requirement ID (`logs/gap-report.md` and `logs/parking-lot.md`) | `foundry/agents/gap-analyzer.md` |

## Model tiers

The wrapper in `foundry/wrapper.py` sends every call through three tiers:

1. Claude, for everything.
2. Gemini Flash, only for bounded tasks (tests, docs, small fixes), only after Claude hits a limit.
3. Pause: state is saved, the orchestrator sleeps until `resume_at`, then probes Claude and resumes.

Flash commits are labeled `[flash]`. Claude reviews them first after the switch back. No model grades its own work.

## Install

Needs Python 3.10 or newer, git, and the `claude` CLI signed in. The Gemini Flash fallback is optional and needs `GEMINI_API_KEY` in your environment.

```bash
git clone https://github.com/sankaranainar-p/agent-foundry
cd agent-foundry
pip install -e ".[dev]"
python -m pytest -q
```

## Quickstart

A worked example is in [examples/spanslo](examples/spanslo/README.md). The short version, on a git repo that already has an approved `spec.md`:

```bash
python3 scripts/init_project.py /path/to/project       # copies templates, writes project.yaml
# edit project.yaml: commands.build, commands.test, commands.smoke, protected_paths
python -m foundry plan /path/to/project                # writes tasks.proposed.md; review it, then move it over tasks.md
python -m foundry run /path/to/project --no-wait       # works the queue in worktrees
python -m foundry audit /path/to/project               # gap report against the spec
```

Results land on the branch `foundry/integration`. Your checked out branch is never touched. You review and merge that branch yourself. Run logs, evidence and the gap report are under `<project>/logs/`.

## Use it on a project

```bash
python3 scripts/init_project.py /path/to/your/project
```

This copies the templates into the project, creates or extends its `.gitignore` (`logs/`, `.venv/`, `__pycache__/`, `.env`, `*.env`, `.DS_Store`; existing lines are kept), and writes a `project.yaml` you fill in (build, test, run, and smoke commands, `agents.developer.mode`, `context`, `protected_paths`, and allowed models). Then:

1. Run the design phase in Claude Code with EGM: `/egm:egm-brainstorm`, `/egm:egm-draft`, `/egm:egm-review`.
2. Save the approved design as `spec.md` and `plan.md` in the project (or point `context.docs` in `project.yaml` at your existing design docs such as `docs/design.md` and `CLAUDE.md`).
3. Run the Planner to produce `tasks.proposed.md`, review it, and move it over `tasks.md`.
4. Run the orchestrator, then run the Gap Analyzer at the milestone boundary.

## Layout

```
foundry/
  agents/     built-in role files (planner, developer, reviewer, playwright, gap-analyzer)
  templates/  constitution, spec, plan, tasks, project.yaml
  wrapper.py, adapters.py, runners.py, cli.py, tasks.py, orchestrator.py
scripts/      init_project.py, smoke_adapters.py
tests/        unit and integration tests
examples/     worked example (spanslo)
```

## CLI & Behavior

```bash
python -m foundry plan <project_dir>                              # writes tasks.proposed.md and validates it
python -m foundry run <project_dir> [--milestone M] [--no-wait] [--sync-main]   # works the queue; sleeps through rate limits unless --no-wait
python -m foundry audit <project_dir>                             # runs the Gap Analyzer -> logs/gap-report.md (+ logs/parking-lot.md)
```

- **Isolated git worktrees:** All branch creation, patch application, tool-assisted edits, `build`/`test` verification, smoke runs, and merges into `foundry/integration` happen inside temporary git worktrees. Your working tree and checked-out branch are never touched.
- **Developer modes:** Set `agents.developer.mode: agent` in `project.yaml` to run Claude Code (`claude -p --permission-mode acceptEdits --allowedTools ...`) directly inside the task worktree, or `mode: diff` (default) for text-in/unified-diff-out. When the wrapper is on the fallback tier, it automatically uses `diff` mode.
- **Deterministic build & test gate:** When `commands.build` or `commands.test` is set in `project.yaml`, `Runners.verify` runs them in the task worktree before the reviewer is called. Any failure routes the command output back to the developer as retry notes and saves `verify.txt` in the attempt folder.
- **Configurable context & docs:** `context.include`, `context.exclude`, and `context.max_chars` filter repository snapshots (lockfiles, binaries, and licenses are excluded by default). `context.docs.{constitution,spec,plan}` can point to existing project files (such as `CLAUDE.md` and `docs/design.md`), and `<project>/agents/<role>.md` overrides any built-in role prompt.
- **Task validation, dependencies & milestones:** Before `run` starts, `validate_tasks` checks that every task has `Satisfies:` requirement IDs (verified against the spec when the spec defines `R-...` IDs), `Done when:`, `Status:`, and valid `Depends on:` references. Tasks can declare `Depends on: T-001` and `Milestone: W5` (`--milestone W5` filters the queue).
- **Idempotent crash recovery:** A task counts as already merged only when `foundry/integration` holds the orchestrator's `Merge T-xxx: title` commit for it. If a run merged a task and crashed before updating `tasks.md`, the next run finds that merge commit and marks the task `done` instead of redoing it.
- **Patch application:** In `diff` mode the reply must contain a unified diff. The runner recounts the hunk headers and tries a chain of `git apply` options, including `--unidiff-zero`. A reply with no diff block fails the attempt.
- **Focused context:** With `context.mode: focused`, small files, non-source files and files in a task's directories stay in full. Other source files are reduced to declarations, with a notice in the prompt. Name the paths a task needs in its Inputs.
- **Rate limits:** On a Claude limit the orchestrator saves state, sleeps until `resume_at`, probes Claude and resumes. Tasks that Flash must not do (unbounded work) wait for Claude. `--no-wait` stops instead of sleeping.
- **Observability:** Every model call logs `role`, `duration_s`, `prompt_chars`, `reply_chars`, and (when reported by `claude`) `cost_usd`, `input_tokens`, `output_tokens`, and `num_turns` to `logs/calls.jsonl`, and accumulates `cost_usd` in `logs/state.json` and `logs/status.md`.
- **Smoke and evidence:** `has_ui: false` runs `commands.smoke` after review and records `stdout.txt`, `stderr.txt` and `exit_code.txt` under `logs/evidence/<task-id>/`. The Gap Analyzer reads that evidence.

## Limits

- `has_ui: true` (Playwright browser flows) is not implemented. It raises `NotImplementedError`.
- It reads your repository and sends it to model providers. Do not run it on code you may not share.
- Runs use your own Claude subscription or key. A large milestone can hit limits and pause for hours. The spanslo pilot cost roughly 64 US dollars in model usage across its milestones.
- If the size cap cuts a file a task names, the task is blocked before any model call. Name the paths a task needs in its Inputs, or raise `context.max_chars`.
- `run` stops if `foundry/integration` is behind `main`. Pass `--sync-main`, or merge it yourself.
- The Gap Analyzer is a model. It follows the end to end evidence rule, but you still read its report.
- The output is code a model wrote and a model reviewed. You still review and merge it.

## Rules

- API keys come from environment variables only. Never commit them.
- Agents never add scope. Work with no requirement ID goes to a parking lot for you to decide.
- EGM and Spec Kit are dependencies. Install them, do not copy their files here.

## Documentation

- [Contributing](CONTRIBUTING.md), [Security](SECURITY.md), [Changelog](CHANGELOG.md)
- [Worked example](examples/spanslo/README.md)

## Cite

See [CITATION.cff](CITATION.cff).

## Disclaimer

agent-foundry is an independent personal project. It is not affiliated with, endorsed by or derived from any employer's systems, data or designs.

## License

Apache License 2.0. See `LICENSE`.

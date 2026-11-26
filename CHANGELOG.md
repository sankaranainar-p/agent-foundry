# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are not stable before 1.0.

## [0.1.0] - 2026-11-26

First public release. It was used to build the first release of [spanslo](https://github.com/sankaranainar-p/spanslo) (weeks 5 to 8 of its plan).

### Added
- Pipeline of Planner, Developer, verify gate, PR Reviewer, smoke run and Gap Analyzer, with role files that a project can override.
- CLI: `plan`, `run` and `audit`.
- Isolated git worktree per task. Only the orchestrator merges into `foundry/integration`, with `Merge T-xxx: title` commits.
- Developer modes: `agent` (Claude Code with tools) and `diff` (text in, unified diff out).
- Patch application chain with hunk header recount and a `--unidiff-zero` fallback.
- Deterministic gate: `commands.build`, `commands.test` and per task Verify steps run before review.
- Smoke command after review, with stdout, stderr and exit code saved under `logs/evidence/<task>/`.
- Model tiers: Claude, Gemini Flash for bounded tasks only, then pause and resume at the time the limit lifts.
- Retry cap of 3, then the task is blocked and the queue halts.
- Context settings: include and exclude lists, size cap, and `focused` mode that keeps declarations only for files a task does not need.
- Task validation: requirement IDs, done checks, dependencies and milestones.
- Gap Analyzer with evidence inlined and replies validated, writing `logs/gap-report.md` and `logs/parking-lot.md`.
- Call logs with sizes, duration, tokens and cost in `logs/calls.jsonl`, plus `logs/status.md`.
- `scripts/init_project.py` and a worked example for spanslo.

### Known limits
- `has_ui: true` (Playwright browser flows) is not implemented and raises `NotImplementedError`.
- The size cap can cut a file a task needs. Name the paths a task needs in its Inputs.
- A blocked task does not pass the reviewer notes into its next run.
- The Gap Analyzer can report a requirement as covered by unit tests when no end to end run exists.
- Run state assumes `foundry/integration` and `main` are kept in step by you.

### Planned
- Gap Analyzer rule: one end to end run per command.
- Warning when the size cap cuts a file a task needs.
- Refuse or merge when `main` has commits that `foundry/integration` lacks.
- Pass a blocked task's reviewer notes into its next run.

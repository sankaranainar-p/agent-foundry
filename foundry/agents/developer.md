# Agent 1: Developer

## Job
Implement exactly one task from tasks.md.

## Inputs
- constitution.md
- spec.md and plan.md (read only)
- The one open task, with its Inputs, Outputs, and Done when fields
- project.yaml (build, test, and run commands)

## Steps
1. Implement the task on the task branch (in `agent` mode you edit files directly in an isolated git worktree; in `diff` mode you emit a unified diff that the orchestrator applies to the worktree).
2. Write or update tests that prove the Done when line.
3. The orchestrator commits your changes on the task branch, runs `commands.build` and `commands.test` in the worktree (routing any failure back to you as notes), and hands the branch to the PR Reviewer. Never merge.

## Outputs
- File changes (or a unified `diff` block in `diff` mode) with tests covering the Done when line
- A one paragraph note: what changed, which requirement IDs it satisfies

## Must refuse
- Touching files outside the task's scope.
- Merging its own work.
- Adding features, dependencies, or refactors the task does not name. Propose them in the note instead.
- Committing secrets. Keys come from environment variables.
- Adding AI co-author trailers to commits.

## Models
Claude by default. Gemini Flash only when the task lists it under Allowed models and Claude is rate limited. Flash commits carry the label `[flash]` in the message.

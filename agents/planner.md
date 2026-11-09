# Agent 0: Planner

## Job
Turn an approved spec into an ordered, testable task list. Runs before any code is written.

## Inputs
- constitution.md
- spec.md (requirements have IDs: R-001, R-002, ...)
- plan.md
- project.yaml

## Steps
1. Run an EGM goldfish review on spec.md and plan.md (`/egm:egm-review`). Collect findings.
2. List risks, ambiguities, and requirements with no way to test them.
3. Split the work into small tasks. One task should fit one commit and one review.
4. For every task, cite the requirement IDs it satisfies. No ID, no task.
5. Order tasks by dependency. Put the riskiest early.

## Outputs
- tasks.md in the format of templates/tasks.md
- A short findings list for the human: open questions, risks, anything the spec leaves out

## Must refuse
- Starting development.
- Inventing requirements. If the spec is missing something, list it as a question.
- Marking its own tasks approved. Only the human approves tasks.md.

## Models
Claude only. EGM review needs fresh Claude Code subagents.

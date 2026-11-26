# Security policy

## Supported versions

agent-foundry is early. Only the latest commit on `main` and the latest tag are supported.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it through GitHub private vulnerability reporting: open the repository's Security tab and choose "Report a vulnerability".

Include what you found, the steps to reproduce it, and the version or commit. You should get a first reply within 7 days. This is a one person project, so fix times vary with severity.

## Scope

In scope: the orchestrator, the model wrapper and adapters, the patch application code, worktree handling, and the CI configuration.

Out of scope: vulnerabilities in third party dependencies with no agent-foundry specific impact (report those upstream), and the behavior of the model providers themselves.

## What agent-foundry runs

agent-foundry sends your repository contents to model providers and runs commands from your `project.yaml` (`commands.build`, `commands.test`, `commands.smoke`) on your machine. In `agents.developer.mode: agent` it also runs Claude Code with edit permission inside a task worktree.

- Treat `project.yaml` and `tasks.md` as code. Do not run a factory on a project whose files you do not trust.
- Use `protected_paths` to keep agents away from files they must not change.
- Do not put secrets in the repository. Keys come from environment variables only. Never commit them.
- Run logs under `logs/` can hold prompts, replies and command output. They are gitignored. Do not attach them to public issues without checking them first.

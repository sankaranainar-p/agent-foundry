# Contributing

Thanks for looking. agent-foundry is early, and issues and discussion are the most useful contribution right now.

## Build and test

```
pip install -e ".[dev]"
python -m pytest -q
```

Needs Python 3.10 or newer. CI runs 3.10, 3.11 and 3.12.

## Ground rules

- The orchestrator is the only thing that merges into `foundry/integration`. Agents never merge.
- Everything that touches git runs in a temporary worktree. Do not touch the user's working tree or checked out branch.
- Agents never add scope. Work with no requirement ID goes to the parking lot.
- No model grades its own work. Keep the reviewer in a fresh context.
- API keys come from environment variables only.
- Tests come with every behavior change. Mock the model calls. Tests must not need network access or a real key.
- A new dependency needs a reason in the pull request.
- Use public data only. No employer data, names, code or prompts.
- Do not add AI co-author or attribution trailers to commits.

## Pull requests

- Keep one change per pull request.
- Say what problem it fixes. Add a test that fails without the fix.
- CI must pass.

## Ideas wanted

See the "Limits" section of the README and the "Planned" list in `CHANGELOG.md`.

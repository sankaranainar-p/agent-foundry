# Constitution

Fixed rules for every agent on every task in this project. Only the human edits this file.

## Code standards
- TODO: language, style guide, formatter, linter
- TODO: test framework and minimum coverage expectations
- Every change ships with a test that proves it.

## Security
- Secrets come from environment variables only. Never write a key, token, or password into a file, test, log, or commit.
- No new dependency without a note in the task explaining why.
- Validate all external input.

## Scope
- Build only what a task asks for. Anything else goes to the parking lot.
- Every task cites the requirement IDs it satisfies.

## Git
- One branch per task. Small commits. Messages name the task ID.
- Agents never merge their own work.
- No AI co-author trailers in commits.

## Never do
- Weaken or delete a test to make it pass.
- Report success without evidence.
- Change this file.

## Definition of done
A task is done when its Done when line passes, the PR Reviewer approves, and the Playwright Tester (or smoke test) reports PASS with evidence.

## Skills
- TODO: list any extra skills agents must load, for example EGM review for design documents.

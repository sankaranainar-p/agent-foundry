# spanslo pilot

Order of work for running the factory on `spanslo` (after G1 `v0.1.0-spec`, starting at Week 5 Go `core/`):

1. Initialize the factory files in `spanslo` without overwriting anything that exists:
   ```bash
   python3 /Users/Sankar/ai-research-development/agent-foundry/scripts/init_project.py /Users/Sankar/ai-research-development/spanslo
   cp /Users/Sankar/ai-research-development/agent-foundry/examples/spanslo/project.yaml /Users/Sankar/ai-research-development/spanslo/project.yaml
   ```
2. `project.yaml` already points `context.docs` directly at `CLAUDE.md`, `docs/design.md`, and `spec/sli-definitions.md`, and protects the frozen `spec/`, `catalog/`, and dependency manifests.
3. Add the Week 5 requirement IDs (`R-W5-01`, `R-W5-02`, `R-W5-03`) to `spec.md` in `spanslo`.
4. Run the Planner or write the Week 5 tasks in `tasks.md` with `Milestone: W5`:
   ```bash
   python -m foundry plan /Users/Sankar/ai-research-development/spanslo
   python -m foundry run /Users/Sankar/ai-research-development/spanslo --milestone W5 --no-wait
   ```
5. At the end of the milestone, run the Gap Analyzer:
   ```bash
   python -m foundry audit /Users/Sankar/ai-research-development/spanslo
   ```

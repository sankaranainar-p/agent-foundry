# spanslo pilot

Order of work for the first run of the factory:

1. In the spanslo repo, run EGM: `/egm:egm-brainstorm`, `/egm:egm-draft`, `/egm:egm-review design_draft.md`.
2. Save the approved design as `spec.md` and `plan.md`. Give every requirement an ID (R-001, R-002, ...).
3. `python3 /Users/Sankar/ai-research-development/agent-foundry/scripts/init_project.py /Users/Sankar/ai-research-development/spanslo`
   (existing files are skipped, so it will not overwrite your spec).
4. Fill in `project.yaml`. Settle `has_ui`.
5. Run the Planner agent, approve `tasks.md`, run the orchestrator on the first three tasks.
6. Run the Gap Analyzer at the first milestone.

"""Parse and update tasks.md.

Format, one block per task:

    ## T-004: Add fallback to Gemini Flash
    Satisfies: R-007, R-009
    Inputs: ...
    Outputs: ...
    Done when: ...
    Allowed models: Claude, Gemini Flash
    Depends on: T-002, T-003      (optional; earlier tasks only)
    Milestone: M1                 (optional; `foundry run --milestone M1` works only these)
    Status: open

The orchestrator only ever changes the Status line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

HEADING = re.compile(r"^##\s+(T-\d+):\s*(.+?)\s*$")
FIELD = re.compile(r"^([A-Za-z][A-Za-z ]*?):\s*(.*)$")


@dataclass
class Task:
    id: str
    title: str
    fields: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.fields.get("Status", "open").strip().lower()

    @property
    def requirements(self) -> list[str]:
        raw = self.fields.get("Satisfies", "")
        return [r.strip() for r in raw.split(",") if r.strip()]

    @property
    def allowed_models(self) -> list[str]:
        raw = self.fields.get("Allowed models", "Claude")
        return [m.strip().lower() for m in raw.split(",") if m.strip()]

    @property
    def bounded(self) -> bool:
        """A task may run on the fallback model only if it lists a non Claude model."""
        return any(m != "claude" for m in self.allowed_models)

    @property
    def depends_on(self) -> list[str]:
        raw = self.fields.get("Depends on", "")
        return [d.strip() for d in re.split(r"[,\s]+", raw) if d.strip() and d.strip().lower() != "none"]

    @property
    def milestone(self) -> str:
        return self.fields.get("Milestone", "").strip()


def parse_tasks(text: str) -> list[Task]:
    tasks: list[Task] = []
    current: Optional[Task] = None
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            current = Task(id=m.group(1), title=m.group(2))
            tasks.append(current)
            continue
        if current is None:
            continue
        f = FIELD.match(line)
        if f:
            current.fields[f.group(1).strip()] = f.group(2).strip()
    return tasks


def in_milestone(task: Task, milestone: Optional[str]) -> bool:
    return not milestone or task.milestone.casefold() == milestone.casefold()


def next_open_task(tasks: list[Task], milestone: Optional[str] = None) -> Optional[Task]:
    """First open task in the milestone whose dependencies are all done."""
    done = {t.id for t in tasks if t.status == "done"}
    for t in tasks:
        if t.status == "open" and in_milestone(t, milestone) and set(t.depends_on) <= done:
            return t
    return None


STATUSES = {"open", "done", "blocked"}
REQ_ID = re.compile(r"\bR-[A-Za-z0-9-]*\d\b")


def validate_tasks(text: str, spec_text: str = "") -> list[str]:
    """Problems that make tasks.md unsafe to run. Empty list means valid.
    Requirement IDs are checked against spec_text only when it defines any R-IDs."""
    tasks, problems = parse_tasks(text), []
    if not tasks:
        return ["no tasks found (headings must look like `## T-001: Title`)"]
    known_reqs = set(REQ_ID.findall(spec_text))
    seen: set[str] = set()
    for t in tasks:
        where = f"{t.id}:"
        if t.id in seen:
            problems.append(f"{where} duplicate task ID")
        if not t.requirements:
            problems.append(f"{where} no `Satisfies:` requirement IDs (no ID, no task)")
        elif known_reqs and (unknown := [r for r in t.requirements if r not in known_reqs]):
            problems.append(f"{where} cites requirement IDs not in the spec: {', '.join(unknown)}")
        if not t.fields.get("Done when", "").strip():
            problems.append(f"{where} no `Done when:` line")
        if "Status" not in t.fields:
            problems.append(f"{where} no `Status:` line")
        elif t.status not in STATUSES:
            problems.append(f"{where} status {t.status!r} is not one of {', '.join(sorted(STATUSES))}")
        if bad := [d for d in t.depends_on if d not in seen]:
            problems.append(f"{where} depends on unknown or later tasks: {', '.join(bad)}")
        seen.add(t.id)
    return problems


def set_status(text: str, task_id: str, status: str) -> str:
    """Return text with the Status line of one task changed. Nothing else is touched."""
    out: list[str] = []
    in_task = False
    changed = False
    for line in text.splitlines():
        m = HEADING.match(line)
        if m:
            in_task = m.group(1) == task_id
        elif in_task and line.startswith("Status:"):
            line = f"Status: {status}"
            changed = True
        out.append(line)
    if not changed:
        raise KeyError(f"task {task_id} has no Status line")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")

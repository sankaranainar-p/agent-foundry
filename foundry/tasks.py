"""Parse and update tasks.md.

Format, one block per task:

    ## T-004: Add fallback to Gemini Flash
    Satisfies: R-007, R-009
    Inputs: ...
    Outputs: ...
    Done when: ...
    Allowed models: Claude, Gemini Flash
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


def next_open_task(tasks: list[Task]) -> Optional[Task]:
    for t in tasks:
        if t.status == "open":
            return t
    return None


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

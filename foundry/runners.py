"""One runner per agent. Each builds a prompt from agents/<role>.md plus project files, sends it
through ModelWrapper, and returns the result.

Providers are text in, text out, so the developer asks for a unified diff and this module
applies and commits it with git. Nothing here merges.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
from pathlib import Path

from .orchestrator import AgentFailed
from .tasks import Task
from .wrapper import ModelWrapper

AGENTS = Path(__file__).resolve().parent.parent / "agents"
SNAPSHOT_CHARS = 60_000  # ponytail: whole-file snapshot with a cap, no retrieval; add when repos outgrow it
DIFF_BLOCK = re.compile(r"```(?:diff|patch)\n(.*?)\n```", re.S)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AgentFailed(f"git {' '.join(args[:2])} failed: {(p.stderr or p.stdout)[:300]}")
    return p.stdout


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30].strip("-")


class Runners:
    def __init__(self, project_dir: str | Path, wrapper: ModelWrapper, cfg: dict):
        self.dir = Path(project_dir)
        self.wrapper = wrapper
        self.cfg = cfg
        self.base = _git(self.dir, "rev-parse", "--abbrev-ref", "HEAD").strip() if (self.dir / ".git").exists() else ""

    # prompt building

    def _read(self, name: str) -> str:
        p = self.dir / name
        return p.read_text() if p.exists() else ""

    def _prompt(self, role: str, **sections: str) -> str:
        parts = [(AGENTS / f"{role}.md").read_text()]
        parts += [f"\n=== {k.replace('_', ' ')} ===\n{v}" for k, v in sections.items() if v]
        return "\n".join(parts)

    def _task_text(self, task: Task) -> str:
        return f"{task.id}: {task.title}\n" + "\n".join(f"{k}: {v}" for k, v in task.fields.items())

    def _snapshot(self) -> str:
        out, used = [], 0
        for name in _git(self.dir, "ls-files").splitlines():
            try:
                body = (self.dir / name).read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if used + len(body) > SNAPSHOT_CHARS:
                out.append(f"--- {name} (omitted, size cap)")
                continue
            used += len(body)
            out.append(f"--- {name}\n{body}")
        return "\n".join(out)

    # agents

    def planner(self) -> str:
        return self.wrapper.call(
            self._prompt("planner", constitution=self._read("constitution.md"), spec=self._read("spec.md"),
                         plan=self._read("plan.md"), project_yaml=self._read("project.yaml")),
            bounded=False,
        ).text

    def developer(self, task: Task, notes: str = "") -> str:
        """Returns the branch name. Raises AgentFailed when the model gives no usable diff."""
        prompt = self._prompt(
            "developer",
            constitution=self._read("constitution.md"), spec=self._read("spec.md"), plan=self._read("plan.md"),
            task=self._task_text(task), notes_from_previous_attempt=notes, repository=self._snapshot(),
            output_format="Reply with ONE unified diff in a ```diff fenced block (paths relative to the repo root, "
                          "git apply compatible), then a one paragraph note. No other code blocks.",
        )
        result = self.wrapper.call(prompt, bounded=task.bounded)
        m = DIFF_BLOCK.search(result.text)
        if not m:
            raise AgentFailed("no ```diff block in the developer reply")
        branch = f"{task.id.lower()}-{_slug(task.title)}"
        # A retry starts a fresh branch from base: the prompt snapshot is base, so the diff must be too.
        _git(self.dir, "branch", "-D", branch, check=False)
        with self._on_branch(branch, create=True):
            patch = self.dir / "logs" / f"{task.id}.patch"
            patch.parent.mkdir(exist_ok=True)
            patch.write_text(m.group(1) + "\n")
            _git(self.dir, "apply", "--index", str(patch))  # stages exactly the patched files
            msg = f"{task.id}: {task.title}" + (f" {result.label}" if result.label else "")
            _git(self.dir, "commit", "-q", "-m", msg)
        return branch

    def reviewer(self, task: Task, branch: str) -> tuple[bool, str]:
        """Fresh context: diff, task, constitution, spec. No developer notes."""
        diff = _git(self.dir, "diff", f"{self.base}...{branch}")
        text = self.wrapper.call(
            self._prompt("reviewer", constitution=self._read("constitution.md"), spec=self._read("spec.md"),
                         task=self._task_text(task), diff=diff),
            bounded=False,
        ).text
        return text.lstrip().upper().startswith("APPROVE"), text

    def playwright(self, task: Task, branch: str) -> tuple[bool, str]:
        if self.cfg.get("has_ui"):
            raise NotImplementedError("has_ui: true (Playwright browser flows) is not implemented yet")
        cmd = (self.cfg.get("commands") or {}).get("smoke", "")
        if not cmd:
            raise AgentFailed("commands.smoke is empty in project.yaml")
        with self._on_branch(branch):
            p = subprocess.run(cmd, shell=True, cwd=self.dir, capture_output=True, text=True)
        ev = self.dir / "logs" / "evidence" / task.id
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "stdout.txt").write_text(p.stdout)
        (ev / "stderr.txt").write_text(p.stderr)
        (ev / "exit_code.txt").write_text(f"{p.returncode}\n")
        return p.returncode == 0, f"smoke exit {p.returncode}; evidence in {ev}\n{p.stderr[-500:]}"

    def gap_analyzer(self) -> str:
        ev = self.dir / "logs" / "evidence"
        files = "\n".join(str(f.relative_to(self.dir)) for f in sorted(ev.rglob("*")) if f.is_file()) if ev.exists() else ""
        return self.wrapper.call(
            self._prompt("gap-analyzer", spec=self._read("spec.md"), plan=self._read("plan.md"),
                         tasks=self._read("tasks.md"), evidence_files=files, repository=self._snapshot()),
            bounded=False,
        ).text

    # git helpers

    @contextlib.contextmanager
    def _on_branch(self, branch: str, create: bool = False):
        _git(self.dir, "checkout", *(["-q", "-b"] if create else ["-q"]), branch)
        try:
            yield
        finally:
            _git(self.dir, "checkout", "-q", self.base)

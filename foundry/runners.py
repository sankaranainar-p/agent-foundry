"""One runner per agent. Each builds a prompt from agents/<role>.md plus project files, sends it
through ModelWrapper, and returns the result.

Providers are text in, text out, so the developer asks for a unified diff and this module
applies and commits it with git. Task branches start from foundry/integration. Agents never merge: Runners.merge is called by
the orchestrator, and only into foundry/integration, never into the base branch.
"""

from __future__ import annotations

import contextlib
import fnmatch
import posixpath
import re
import subprocess
from pathlib import Path

from .orchestrator import AgentFailed
from .tasks import Task
from .wrapper import ModelWrapper

AGENTS = Path(__file__).resolve().parent.parent / "agents"
SNAPSHOT_CHARS = 60_000  # ponytail: whole-file snapshot with a cap, no retrieval; add when repos outgrow it
INTEGRATION = "foundry/integration"
PROTECTED_DEFAULT = ["constitution.md", "spec.md", "plan.md", "tasks.md", "project.yaml", "agents/*", ".github/*", ".env*"]
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
        self.integ = INTEGRATION
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

    def _tip(self) -> str:
        """Integration branch if it exists, else the base branch."""
        has = _git(self.dir, "rev-parse", "--verify", "--quiet", self.integ, check=False).strip()
        return self.integ if has else self.base

    def _snapshot(self) -> str:
        ref, out, used = self._tip(), [], 0
        for name in _git(self.dir, "ls-tree", "-r", "--name-only", ref).splitlines():
            try:
                body = _git(self.dir, "show", f"{ref}:{name}")
            except (UnicodeDecodeError, AgentFailed):
                continue
            if used + len(body) > SNAPSHOT_CHARS:
                out.append(f"--- {name} (omitted, size cap)")
                continue
            used += len(body)
            out.append(f"--- {name}\n{body}")
        return "\n".join(out)

    def ensure_integration(self) -> None:
        """Create foundry/integration from the base branch, or reuse it."""
        if self._tip() == self.base:
            _git(self.dir, "branch", self.integ, self.base)

    def integration_line(self) -> str:
        n = _git(self.dir, "rev-list", "--merges", "--count", f"{self.base}..{self.integ}", check=False).strip() or "0"
        return f"integration branch: {self.integ} ({n} task merges)"

    def _protected_hits(self, patch: Path) -> list[str]:
        pats = [p.casefold() for p in self.cfg.get("protected_paths") or PROTECTED_DEFAULT]
        toks = _git(self.dir, "apply", "--numstat", "-z", str(patch)).split("\0")
        paths, i = [], 0
        while i < len(toks):
            parts = toks[i].split("\t", 2)
            if len(parts) == 3 and parts[2] == "":  # rename or copy: old and new follow
                paths += toks[i + 1:i + 3]
                i += 3
            else:
                paths += [parts[2]] if len(parts) == 3 else []
                i += 1
        hits = []
        for raw in paths:
            path = posixpath.normpath(raw).casefold()
            if any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(posixpath.basename(path), p) for p in pats):
                hits.append(raw)
        return hits

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
        self.ensure_integration()
        branch = f"{task.id.lower()}-{_slug(task.title)}"
        patch = self.dir / "logs" / f"{task.id}.patch"
        patch.parent.mkdir(exist_ok=True)
        patch.write_text(m.group(1) + "\n")
        if hits := self._protected_hits(patch):
            raise AgentFailed(f"diff touches protected paths, remove these changes: {', '.join(hits)}")
        # A retry starts a fresh branch from the integration tip: the prompt snapshot is the tip, so the diff must be too.
        _git(self.dir, "branch", "-D", branch, check=False)
        with self._on_branch(branch, create=True, start=self.integ):
            _git(self.dir, "apply", "--index", str(patch))  # stages exactly the patched files
            msg = f"{task.id}: {task.title}" + (f" {result.label}" if result.label else "")
            _git(self.dir, "commit", "-q", "-m", msg)
        return branch

    def reviewer(self, task: Task, branch: str) -> tuple[bool, str]:
        """Fresh context: diff, task, constitution, spec. No developer notes."""
        diff = _git(self.dir, "diff", f"{self.integ}...{branch}")
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

    def merge(self, task: Task, branch: str) -> tuple[bool, str]:
        """Called by the orchestrator, never by an agent. Merges into foundry/integration only."""
        _git(self.dir, "checkout", "-q", self.integ)
        try:
            p = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {task.id}: {task.title}", branch],
                               cwd=self.dir, capture_output=True, text=True)
            if p.returncode != 0:
                text = (p.stdout + p.stderr)[:2000]
                _git(self.dir, "merge", "--abort", check=False)
                return False, f"merge conflict into {self.integ}:\n{text}"
            return True, ""
        finally:
            _git(self.dir, "checkout", "-q", self.base)

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
    def _on_branch(self, branch: str, create: bool = False, start: str = ""):
        _git(self.dir, "checkout", *(["-q", "-b"] if create else ["-q"]), branch, *([start] if start else []))
        try:
            yield
        finally:
            _git(self.dir, "checkout", "-q", self.base)

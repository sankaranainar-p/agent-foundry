"""One runner per agent. Each builds a prompt from agents/<role>.md (project override first, else
the built-in foundry/agents/<role>.md) plus project files, sends it through ModelWrapper, and
returns the result.

All git branch work happens inside throwaway git worktrees under logs/.worktrees/, so the user's
working tree and checked-out branch are never touched. Task branches start from foundry/integration.
Agents never merge: Runners.merge is called by the orchestrator, and only into foundry/integration,
never into the base branch.
"""

from __future__ import annotations

import contextlib
import fnmatch
import posixpath
import re
import shutil
import string
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional

from .orchestrator import AgentFailed
from .tasks import Task
from .wrapper import TIER_PRIMARY, ModelWrapper

AGENTS = Path(__file__).resolve().parent / "agents"
SNAPSHOT_CHARS = 60_000
INTEGRATION = "foundry/integration"
PROTECTED_DEFAULT = [
    "constitution.md", "spec.md", "plan.md", "tasks.md", "project.yaml", "agents/*", ".github/*", ".env*",
]
SNAPSHOT_EXCLUDE_DEFAULT = [
    "*.lock", "uv.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "go.sum",
    "*.min.js", "*.min.css", "*.map", "*.svg", "*.png", "*.jpg", "*.pdf", "*.wasm",
    "LICENSE", "LICENSE.*",
]
DOC_DEFAULTS = {
    "constitution": ["constitution.md"],
    "spec": ["spec.md"],
    "plan": ["plan.md"],
}
APPROVE_LINE = re.compile(
    r"^(?:verdict\s*:\s*)?approved?\b(?!.*\b(?:but|however|except|unless|although|not|changes|reject\w*)\b)"
)
DIFF_BLOCK = re.compile(r"```(?:diff|patch)\n(.*?)\n```", re.S)


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AgentFailed(f"git {' '.join(args[:2])} failed: {(p.stderr or p.stdout)[:300]}")
    return p.stdout


def parse_verdict(text: str) -> bool:
    """True only for a clear APPROVE on the first non-empty line. Markdown emphasis and
    surrounding punctuation are ignored; anything ambiguous is not approved."""
    line = next((l for l in text.splitlines() if l.strip()), "")
    line = re.sub(r"[*`_]", "", line).strip().strip(string.punctuation + string.whitespace).casefold()
    return bool(APPROVE_LINE.match(line))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30].strip("-")


def _match_any(raw: str, patterns: Iterable[str]) -> bool:
    path = posixpath.normpath(raw).casefold()
    base = posixpath.basename(path)
    for p in patterns:
        pat = p.casefold()
        if fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(base, pat):
            return True
    return False


class Runners:
    def __init__(
        self,
        project_dir: str | Path,
        wrapper: ModelWrapper,
        cfg: dict,
        agent_runner: Optional[Callable[[str, str], str]] = None,
    ):
        self.dir = Path(project_dir)
        self.wrapper = wrapper
        self.cfg = cfg
        self.agent_runner = agent_runner
        self._attempt_dir: Optional[Path] = None
        self.integ = INTEGRATION
        self.base = _git(self.dir, "rev-parse", "--abbrev-ref", "HEAD").strip() if (self.dir / ".git").exists() else ""

    # per attempt logs: logs/<task-id>/attempt-<n>/

    def set_attempt(self, task_id: str, n: int) -> None:
        self._attempt_dir = self.dir / "logs" / task_id / f"attempt-{n}"
        shutil.rmtree(self._attempt_dir, ignore_errors=True)  # a rerun must not leave stale files
        self._attempt_dir.mkdir(parents=True)

    def record(self, name: str, text: str) -> None:
        if self._attempt_dir:
            (self._attempt_dir / name).write_text(text)

    # prompt and context building

    def _read(self, name: str) -> str:
        p = self.dir / name
        return p.read_text() if p.exists() else ""

    def doc_text(self, key: str) -> str:
        """Read constitution, spec, or plan. Uses context.docs.<key> from project.yaml (a path or
        list of paths relative to the repo root) when set, else the default root file."""
        docs = ((self.cfg.get("context") or {}).get("docs") or {}).get(key)
        if isinstance(docs, str):
            paths = [docs] if docs.strip() else []
        elif isinstance(docs, list):
            paths = [str(p) for p in docs if str(p).strip()]
        else:
            paths = DOC_DEFAULTS.get(key, [f"{key}.md"])
        parts = []
        for rel in paths:
            body = self._read(rel)
            if body:
                parts.append(body if len(paths) == 1 else f"--- {rel}\n{body}")
        return "\n\n".join(parts)

    def _role_text(self, role: str) -> str:
        role_cfg = ((self.cfg.get("agents") or {}).get(role) or {})
        if custom := role_cfg.get("prompt"):
            p = self.dir / custom
            if p.exists():
                return p.read_text()
        local = self.dir / "agents" / f"{role}.md"
        if local.exists():
            return local.read_text()
        return (AGENTS / f"{role}.md").read_text()

    def _prompt(self, role: str, **sections: str) -> str:
        parts = [self._role_text(role)]
        parts += [f"\n=== {k.replace('_', ' ')} ===\n{v}" for k, v in sections.items() if v]
        return "\n".join(parts)

    def _task_text(self, task: Task) -> str:
        return f"{task.id}: {task.title}\n" + "\n".join(f"{k}: {v}" for k, v in task.fields.items())

    def _tip(self) -> str:
        """Integration branch if it exists, else the base branch."""
        has = _git(self.dir, "rev-parse", "--verify", "--quiet", self.integ, check=False).strip()
        return self.integ if has else self.base

    def _snapshot(self) -> str:
        ctx = self.cfg.get("context") or {}
        cap = int(ctx.get("max_chars") or SNAPSHOT_CHARS)
        inc = ctx.get("include") or []
        exc = ctx.get("exclude") if "exclude" in ctx else SNAPSHOT_EXCLUDE_DEFAULT
        ref, out, used = self._tip(), [], 0
        for name in _git(self.dir, "ls-tree", "-r", "--name-only", ref).splitlines():
            if inc and not _match_any(name, inc):
                continue
            if exc and _match_any(name, exc):
                continue
            try:
                body = _git(self.dir, "show", f"{ref}:{name}")
            except (UnicodeDecodeError, AgentFailed):
                continue
            if used + len(body) > cap:
                out.append(f"--- {name} (omitted, size cap)")
                continue
            used += len(body)
            out.append(f"--- {name}\n{body}")
        return "\n".join(out)

    def _touched_context(self, branch: str) -> str:
        """Contents of files on `branch` touched since integration, for the reviewer."""
        ctx = self.cfg.get("context") or {}
        cap = int(ctx.get("max_chars") or SNAPSHOT_CHARS)
        names = _git(self.dir, "diff", "--name-only", f"{self.integ}...{branch}").splitlines()
        out, used = [], 0
        for name in names:
            try:
                body = _git(self.dir, "show", f"{branch}:{name}", check=False)
            except UnicodeDecodeError:
                continue
            if not body:
                continue
            if used + len(body) > cap:
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

    def task_branch(self, task: Task) -> str:
        return f"{task.id.lower()}-{_slug(task.title)}"

    def is_merged(self, task: Task) -> bool:
        """True if the task's branch already exists and its tip is an ancestor of integration
        (meaning an earlier run merged it and crashed before updating tasks.md)."""
        if not (self.dir / ".git").exists():
            return False
        branch = self.task_branch(task)
        if not _git(self.dir, "rev-parse", "--verify", "--quiet", branch, check=False).strip():
            return False
        if not _git(self.dir, "rev-parse", "--verify", "--quiet", self.integ, check=False).strip():
            return False
        # Must also have a merge or commit for this task past the base branch, not an empty branch at base.
        ahead = _git(self.dir, "rev-list", "--count", f"{self.base}..{branch}", check=False).strip()
        if not ahead or ahead == "0":
            return False
        rc = subprocess.run(["git", "merge-base", "--is-ancestor", branch, self.integ], cwd=self.dir).returncode
        return rc == 0

    def _protected_patterns(self) -> list[str]:
        return [p.casefold() for p in self.cfg.get("protected_paths") or PROTECTED_DEFAULT]

    def _filter_protected(self, paths: Iterable[str]) -> list[str]:
        pats = self._protected_patterns()
        return [raw for raw in paths if _match_any(raw, pats)]

    def _protected_hits(self, patch: Path) -> list[str]:
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
        return self._filter_protected(paths)

    def _changed_paths_in_worktree(self, wt: Path) -> list[str]:
        tracked = _git(wt, "diff", "--name-only", "-z", "HEAD").split("\0")
        untracked = _git(wt, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        return [p for p in dict.fromkeys(tracked + untracked) if p]

    # agents

    def planner(self) -> str:
        return self.wrapper.call(
            self._prompt("planner", constitution=self.doc_text("constitution"), spec=self.doc_text("spec"),
                         plan=self.doc_text("plan"), project_yaml=self._read("project.yaml")),
            bounded=False,
            role="planner",
        ).text

    def _dev_mode(self) -> str:
        dev_cfg = (self.cfg.get("agents") or {}).get("developer") or {}
        mode = str(dev_cfg.get("mode") or "diff").lower()
        if mode == "agent" and self.agent_runner and self.wrapper.active_tier == TIER_PRIMARY:
            return "agent"
        return "diff"

    def developer(self, task: Task, notes: str = "") -> str:
        """Returns the branch name. Raises AgentFailed when the model gives no usable change."""
        if self._dev_mode() == "agent":
            return self._developer_agent(task, notes)
        return self._developer_diff(task, notes)

    def _developer_diff(self, task: Task, notes: str = "") -> str:
        prompt = self._prompt(
            "developer",
            constitution=self.doc_text("constitution"), spec=self.doc_text("spec"), plan=self.doc_text("plan"),
            task=self._task_text(task), notes_from_previous_attempt=notes, repository=self._snapshot(),
            output_format="Reply with ONE unified diff in a ```diff fenced block (paths relative to the repo root, "
                          "git apply compatible), then a one paragraph note. No other code blocks.",
        )
        result = self.wrapper.call(prompt, bounded=task.bounded, role="developer")
        self.record("developer-reply.md", result.text)
        m = DIFF_BLOCK.search(result.text)
        if not m:
            raise AgentFailed("no ```diff block in the developer reply")
        self.ensure_integration()
        branch = self.task_branch(task)
        patch = self.dir / "logs" / f"{task.id}.patch"
        patch.parent.mkdir(exist_ok=True)
        patch.write_text(m.group(1) + "\n")
        self.record("diff.patch", m.group(1) + "\n")
        if hits := self._protected_hits(patch):
            raise AgentFailed(f"diff touches protected paths, remove these changes: {', '.join(hits)}")
        with self._worktree(branch, create=True, start=self.integ) as wt:
            _git(wt, "apply", "--index", str(patch))  # stages exactly the patched files
            msg = f"{task.id}: {task.title}" + (f" {result.label}" if result.label else "")
            _git(wt, "commit", "-q", "-m", msg)
        return branch

    def _developer_agent(self, task: Task, notes: str = "") -> str:
        """Run a tool-using coding agent inside a throwaway git worktree, then validate and commit."""
        self.ensure_integration()
        branch = self.task_branch(task)
        cmds = self.cfg.get("commands") or {}
        cmd_hint = "\n".join(f"{k}: {v}" for k, v in cmds.items() if v)
        prompt = self._prompt(
            "developer",
            constitution=self.doc_text("constitution"), spec=self.doc_text("spec"), plan=self.doc_text("plan"),
            task=self._task_text(task), commands=cmd_hint, notes_from_previous_attempt=notes,
            instructions="You are working inside an isolated git worktree for this task. Edit files directly using your "
                         "tools. Do NOT run git commit, git checkout, or git merge (the orchestrator commits your "
                         "work). Do NOT touch protected paths. Reply with a short summary of what changed and which "
                         "requirement IDs it satisfies.",
        )
        with self._worktree(branch, create=True, start=self.integ) as wt:
            assert self.agent_runner is not None
            result = self.wrapper.call(
                prompt, bounded=task.bounded, primary=lambda p: self.agent_runner(p, str(wt)), role="developer",
            )
            self.record("developer-reply.md", result.text)
            changed = self._changed_paths_in_worktree(wt)
            if not changed and _git(wt, "rev-list", "--count", f"{self.integ}..HEAD").strip() == "0":
                _git(self.dir, "branch", "-D", branch, check=False)
                raise AgentFailed("developer agent made no file changes in the worktree")
            if hits := self._filter_protected(changed):
                _git(self.dir, "branch", "-D", branch, check=False)
                raise AgentFailed(f"diff touches protected paths, remove these changes: {', '.join(hits)}")
            if changed:
                _git(wt, "add", "-A")
                diff = _git(wt, "diff", "--cached")
                self.record("diff.patch", diff)
                (self.dir / "logs" / f"{task.id}.patch").write_text(diff)
                msg = f"{task.id}: {task.title}" + (f" {result.label}" if result.label else "")
                _git(wt, "commit", "-q", "-m", msg)
            else:
                diff = _git(wt, "diff", f"{self.integ}...HEAD")
                self.record("diff.patch", diff)
        return branch

    def verify(self, task: Task, branch: str) -> tuple[bool, str]:
        """Run commands.build and commands.test inside a worktree for `branch` before review.
        When neither command is configured, verification is a no-op pass."""
        cmds = self.cfg.get("commands") or {}
        steps = [(k, cmds.get(k, "").strip()) for k in ("build", "test") if (cmds.get(k) or "").strip()]
        if not steps:
            return True, ""
        logs: list[str] = []
        with self._worktree(branch) as wt:
            for name, cmd in steps:
                p = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True)
                entry = f"$ {cmd}  ({name}, exit {p.returncode})\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}"
                logs.append(entry)
                if p.returncode != 0:
                    full = "\n".join(logs)
                    self.record("verify.txt", full)
                    tail = (p.stdout + p.stderr)[-2000:]
                    return False, f"commands.{name} failed (exit {p.returncode}):\n$ {cmd}\n{tail}"
        self.record("verify.txt", "\n".join(logs))
        return True, ""

    def reviewer(self, task: Task, branch: str) -> tuple[bool, str]:
        """Fresh context: diff, touched files, task, constitution, spec. No developer notes."""
        diff = _git(self.dir, "diff", f"{self.integ}...{branch}")
        touched = self._touched_context(branch)
        text = self.wrapper.call(
            self._prompt("reviewer", constitution=self.doc_text("constitution"), spec=self.doc_text("spec"),
                         task=self._task_text(task), diff=diff, touched_files=touched),
            bounded=False,
            role="reviewer",
        ).text
        self.record("reviewer-reply.md", text)
        if parse_verdict(text):
            return True, text
        first = next((l for l in text.splitlines() if l.strip()), "").strip()
        if re.sub(r"[*`_]", "", first).strip(string.punctuation + " ").casefold().startswith("verdict: changes requested"):
            return False, text
        return False, ("Reviewer reply had no clear verdict (first line must be `VERDICT: APPROVE` or "
                       f"`VERDICT: CHANGES REQUESTED`), so it counts as not approved. Raw reply:\n{text}")

    def playwright(self, task: Task, branch: str) -> tuple[bool, str]:
        if self.cfg.get("has_ui"):
            raise NotImplementedError("has_ui: true (Playwright browser flows) is not implemented yet")
        cmd = (self.cfg.get("commands") or {}).get("smoke", "")
        if not cmd:
            raise AgentFailed("commands.smoke is empty in project.yaml")
        with self._worktree(branch) as wt:
            p = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True)
        self.record("smoke.txt", f"$ {cmd}\nexit code: {p.returncode}\n--- stdout\n{p.stdout}\n--- stderr\n{p.stderr}")
        ev = self.dir / "logs" / "evidence" / task.id
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "stdout.txt").write_text(p.stdout)
        (ev / "stderr.txt").write_text(p.stderr)
        (ev / "exit_code.txt").write_text(f"{p.returncode}\n")
        return p.returncode == 0, f"smoke exit {p.returncode}; evidence in {ev}\n{(p.stdout + p.stderr)[-1500:]}"

    def merge(self, task: Task, branch: str) -> tuple[bool, str]:
        """Called by the orchestrator, never by an agent. Merges into foundry/integration inside a
        temporary worktree, so the user's working tree and current branch are never touched."""
        with self._worktree(self.integ) as wt:
            p = subprocess.run(["git", "merge", "--no-ff", "-m", f"Merge {task.id}: {task.title}", branch],
                               cwd=wt, capture_output=True, text=True)
            if p.returncode != 0:
                text = (p.stdout + p.stderr)[:2000]
                _git(wt, "merge", "--abort", check=False)
                return False, f"merge conflict into {self.integ}:\n{text}"
            return True, ""

    def gap_analyzer(self) -> str:
        ev = self.dir / "logs" / "evidence"
        files = "\n".join(str(f.relative_to(self.dir)) for f in sorted(ev.rglob("*")) if f.is_file()) if ev.exists() else ""
        return self.wrapper.call(
            self._prompt("gap-analyzer", spec=self.doc_text("spec"), plan=self.doc_text("plan"),
                         tasks=self._read("tasks.md"), evidence_files=files, repository=self._snapshot()),
            bounded=False,
            role="gap-analyzer",
        ).text

    # git worktree helpers

    @contextlib.contextmanager
    def _worktree(self, branch: str, create: bool = False, start: str = ""):
        """Check out `branch` inside a throwaway git worktree under a temporary directory so
        the main working tree and its checked-out branch are never disturbed."""
        _git(self.dir, "worktree", "prune", check=False)
        with tempfile.TemporaryDirectory(prefix="foundry-wt-") as tmp:
            wt = Path(tmp) / "tree"
            if create:
                _git(self.dir, "worktree", "add", "-q", "-B", branch, str(wt), start or self.integ)
            else:
                _git(self.dir, "worktree", "add", "-q", str(wt), branch)
            try:
                yield wt
            finally:
                _git(self.dir, "worktree", "remove", "--force", str(wt), check=False)
                _git(self.dir, "worktree", "prune", check=False)

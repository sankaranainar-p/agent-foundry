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
import os
import posixpath
import re
import shlex
import shutil
import signal
import string
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional

from .orchestrator import AgentFailed
from .tasks import Task, parse_tasks
from .wrapper import TIER_PRIMARY, ModelWrapper

AGENTS = Path(__file__).resolve().parent / "agents"
SNAPSHOT_CHARS = 60_000
INTEGRATION = "foundry/integration"
REJECTED_HEADING = "Rejected attempt: do not reapply as is; fix the cause"
SIBLING_CHARS = 40_000
REJECTED_DIFF_CHARS = 20 * 1024
COMMAND_TIMEOUT_DEFAULT = 300
SHELL_OPERATORS = {";", "&", "&&", "|", "||", ">", ">>", "<"}
VERIFY_ALLOWLIST_DEFAULT = ["go", "uv", "pytest", "npm", "make"]
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
DECL_LINE = re.compile(r"^\s*(?:func|type|var|const|def|class|interface)\b")
COMMENT_LINE = re.compile(r"^\s*(?://|#|/\*|\*)")
DECL_NAME = re.compile(
    r"^\s*(?:"
    r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
    r"|type\s+([A-Za-z_]\w*)"
    r"|(?:var|const)\s+([A-Za-z_]\w*)"
    r"|(?:def|class|interface)\s+([A-Za-z_]\w*)"
    r")"
)
IDENT_TOKEN = re.compile(r"\b[A-Za-z_]\w*\b")
PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9_]+")
DECL_STOPWORDS = {
    "main", "init", "str", "int", "bool", "err", "error", "string", "true", "false", "nil", "none", "self", "cls",
}


def _is_test_file(path: str) -> bool:
    base = posixpath.basename(_norm(path)).casefold()
    return (
        base.endswith(("_test.go", "_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
        or (base.startswith("test_") and base.endswith(".py"))
    )


SMALL_FILE_CHARS = 6_000
SOURCE_EXTS = {".go", ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".rs", ".c", ".h", ".cc", ".cpp", ".rb", ".cs", ".kt", ".swift"}


def _is_source_code(path: str) -> bool:
    return posixpath.splitext(_norm(path))[1].casefold() in SOURCE_EXTS


def _extract_declarations(body: str) -> str:
    """Keep only lines starting with func, type, var, const, def, class, or interface, plus their
    immediately preceding comment line."""
    lines = body.splitlines()
    out: list[str] = []
    added_prev = False
    for idx, line in enumerate(lines):
        if DECL_LINE.match(line):
            if idx > 0 and COMMENT_LINE.match(lines[idx - 1]) and not added_prev:
                out.append(lines[idx - 1])
            out.append(line)
            added_prev = True
        else:
            added_prev = False
    return ("\n".join(out) + "\n") if out else ""


def _declared_identifiers(body: str) -> set[str]:
    names: set[str] = set()
    for line in body.splitlines():
        if m := DECL_NAME.match(line):
            ident = next((g for g in m.groups() if g), "")
            if len(ident) >= 3 and ident.casefold() not in DECL_STOPWORDS and not ident.startswith(("Test", "test_", "Benchmark")):
                names.add(ident)
    return names


def _task_named_paths(task: Task, tracked: Iterable[str]) -> tuple[set[str], set[str]]:
    """Return (focus_files, focus_dirs) from a task's Inputs and Outputs against tracked repo paths."""
    raw = f"{task.fields.get('Inputs', '')}\n{task.fields.get('Outputs', '')}"
    tracked_list = list(tracked)
    tracked_norm = {_norm(p): p for p in tracked_list}
    files: set[str] = set()
    dirs: set[str] = set()
    for p in tracked_list:
        np = _norm(p)
        if np and np in raw:
            files.add(p)
            dirs.add(posixpath.dirname(np))
    for tok in PATH_TOKEN.findall(raw):
        nt = _norm(tok.strip(".,;:()[]`\"'"))
        if nt in tracked_norm:
            files.add(tracked_norm[nt])
            dirs.add(posixpath.dirname(nt))
        elif "/" in nt:
            dirs.add(posixpath.dirname(nt))
    for tok in re.findall(r"[A-Za-z0-9_./-]+/", raw):
        nd = _norm(tok.rstrip("/"))
        if any(posixpath.dirname(_norm(p)) == nd for p in tracked_list):
            dirs.add(nd)
    return files, dirs


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AgentFailed(f"git {' '.join(args[:2])} failed: {(p.stderr or p.stdout)[:300]}")
    return p.stdout


APPLY_STEPS = [[], ["--ignore-whitespace"], ["--unidiff-zero"], ["--unidiff-zero", "--ignore-whitespace"]]


def _git_apply(cwd: Path, *args: str, used: list[str] | None = None) -> str:
    """Patches are normalized by recount_patch first. Try plain, then --ignore-whitespace, then --unidiff-zero
    (hunks ending in added lines with no trailing context are otherwise anchored at EOF), then both.
    Appends the flags of the step that worked to `used`; the last failure propagates."""
    for i, flags in enumerate(APPLY_STEPS):
        try:
            out = _git(cwd, "apply", *flags, *args)
        except AgentFailed:
            if i == len(APPLY_STEPS) - 1:
                raise
            continue
        if used is not None and flags:
            used.append(" ".join(flags))
        return out


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def recount_patch(text: str) -> tuple[str, list[str]]:
    """Rewrite every hunk header so its counts match the body. Returns (patch, warnings), one
    warning per header that was wrong. git apply trusts header counts: too small silently
    truncates, so we never let it see a wrong one."""
    lines = text.split("\n")
    trailing = lines[-1] == ""  # final empty string from the trailing newline is not a body line
    if trailing:
        lines.pop()
    out, warns, i = [], [], 0
    while i < len(lines):
        m = HUNK_HEADER.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j, old, new = i + 1, 0, 0
        new_file = m.group(1) == "0" and m.group(2) == "0"  # nothing to be context for
        body = []
        while j < len(lines) and not lines[j].startswith(("@@", "diff ")) and not (
            lines[j].startswith("--- ") and lines[j + 1 : j + 2] and lines[j + 1].startswith("+++ ")
            and lines[j + 2 : j + 3] and lines[j + 2].startswith("@@")
        ):
            line = lines[j]
            if new_file and line == "":
                line = "+"
                warns.append(f"{lines[i]}: unprefixed empty line in new-file hunk (body line {j - i}) treated as added blank line")
            c = line[:1]
            if c != "\\":
                old += c in ("", " ", "-")
                new += c in ("", " ", "+")
            body.append(line)
            j += 1
        want = (int(m.group(2) or 1), int(m.group(4) or 1))
        if want != (old, new):
            warns.append(f"{lines[i]}: header says -{want[0]} +{want[1]}, body has -{old} +{new}")
        out.append(f"@@ -{m.group(1)},{old} +{m.group(3)},{new} @@{m.group(5)}")
        out += body
        i = j
    return "\n".join(out) + ("\n" if trailing else ""), warns


def parse_verdict(text: str) -> bool:
    """True only for a clear APPROVE on the first non-empty line. Markdown emphasis and
    surrounding punctuation are ignored; anything ambiguous is not approved."""
    line = next((l for l in text.splitlines() if l.strip()), "")
    line = re.sub(r"[*`_]", "", line).strip().strip(string.punctuation + string.whitespace).casefold()
    return bool(APPROVE_LINE.match(line))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30].strip("-")


def _norm(raw: str) -> str:
    """posix separators, `..` and `.` resolved, no leading `./` or `/`."""
    return posixpath.normpath(raw.replace("\\", "/").strip()).lstrip("/")


def _match_any(raw: str, patterns: Iterable[str]) -> bool:
    path = _norm(raw).casefold()
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
        self._prev_dir: Optional[Path] = None
        self.integ = INTEGRATION
        self.base = _git(self.dir, "rev-parse", "--abbrev-ref", "HEAD").strip() if (self.dir / ".git").exists() else ""

    # per attempt logs: logs/<task-id>/attempt-<n>/

    def set_attempt(self, task_id: str, n: int) -> None:
        self._attempt_dir = self.dir / "logs" / task_id / f"attempt-{n}"
        self._prev_dir = self.dir / "logs" / task_id / f"attempt-{n - 1}" if n > 1 else None
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
        text, _ = self._build_prompt(role, **sections)
        return text

    def _build_prompt(self, role: str, **sections: str) -> tuple[str, dict[str, int]]:
        role_text = self._role_text(role)
        sizes: dict[str, int] = {"role": len(role_text)}
        parts = [role_text]
        for k, v in sections.items():
            if v:
                key = k.replace("_", " ")
                sizes[key] = len(v)
                parts.append(f"\n=== {key} ===\n{v}")
        return "\n".join(parts), sizes

    def _call_role(self, role: str, bounded: bool = False, primary: Optional[Callable[[str], str]] = None,
                   **sections: str):
        prompt, sizes = self._build_prompt(role, **sections)
        return self.wrapper.call(prompt, bounded=bounded, primary=primary, role=role, sections=sizes)

    def _rejected_attempt(self) -> dict[str, str]:
        """On attempt > 1: the previous attempt's diff (capped) and failing output tail, as a prompt section.
        The worktree still starts clean from integration; this only tells the developer what failed."""
        if not self._prev_dir:
            return {}
        diff = self._prev_dir / "diff.patch"
        notes = self._prev_dir / "notes.txt"
        parts = []
        if diff.exists() and (text := diff.read_text()):
            if len(text) > REJECTED_DIFF_CHARS:
                text = text[:REJECTED_DIFF_CHARS] + f"\n[diff truncated at {REJECTED_DIFF_CHARS // 1024} KB]\n"
            parts.append(f"--- diff.patch\n{text}")
        if notes.exists() and (text := "\n".join(notes.read_text().splitlines()[-200:])):
            parts.append(f"--- failing output (last 200 lines)\n{text}")
        return {REJECTED_HEADING: "\n".join(parts)} if parts else {}

    def _task_text(self, task: Task) -> str:
        return f"{task.id}: {task.title}\n" + "\n".join(f"{k}: {v}" for k, v in task.fields.items())

    def _tip(self) -> str:
        """Integration branch if it exists, else the base branch."""
        has = _git(self.dir, "rev-parse", "--verify", "--quiet", self.integ, check=False).strip()
        return self.integ if has else self.base

    def _snapshot(self, first: Iterable[str] = (), task: Optional[Task] = None) -> str:
        """Repo files as prompt text. Files in `first` are sent before the rest so the size cap hits them last.
        When context.mode is 'focused' and a task is given, include full text for:
        1. files named in the task's Inputs and Outputs,
        2. other source (non-test) files in the same directories as those files,
        3. any file (such as a test file) that defines helpers/identifiers used by the focus files.
        For every other tracked file under the cap, include only declaration lines (func, type, var,
        const, def, class, interface, plus their preceding comment line) and note that in the prompt."""
        ctx = self.cfg.get("context") or {}
        cap = int(ctx.get("max_chars") or SNAPSHOT_CHARS)
        inc = ctx.get("include") or []
        exc = ctx.get("exclude") if "exclude" in ctx else SNAPSHOT_EXCLUDE_DEFAULT
        focused = str(ctx.get("mode") or "full").strip().lower() == "focused" and task is not None
        ref = self._tip()
        names = _git(self.dir, "ls-tree", "-r", "--name-only", ref).splitlines()

        filtered: list[str] = []
        bodies: dict[str, str] = {}
        for name in names:
            if inc and not _match_any(name, inc):
                continue
            if exc and _match_any(name, exc):
                continue
            try:
                body = _git(self.dir, "show", f"{ref}:{name}")
            except (UnicodeDecodeError, AgentFailed):
                continue
            if "\0" in body:
                continue
            filtered.append(name)
            bodies[name] = body

        full_set: set[str] = set()
        focus_dirs: set[str] = set()
        priority_order: list[str] = []
        if focused and task is not None:
            named_files, focus_dirs = _task_named_paths(task, filtered)
            same_dir_sources = {
                n for n in filtered
                if posixpath.dirname(_norm(n)) in focus_dirs and not _is_test_file(n)
            }
            full_set = set(named_files) | same_dir_sources
            used_idents: set[str] = set()
            for n in full_set:
                used_idents.update(IDENT_TOKEN.findall(bodies.get(n, "")))
            used_idents.update(IDENT_TOKEN.findall(f"{task.title}\n" + "\n".join(task.fields.values())))
            helper_files = {
                n for n in filtered
                if n not in full_set and (_declared_identifiers(bodies[n]) & used_idents)
            }
            full_set |= helper_files
            priority_order = (
                [n for n in filtered if n in named_files]
                + [n for n in filtered if n in same_dir_sources and n not in named_files]
                + [n for n in filtered if n in helper_files]
            )

        first_set = set(first) | set(priority_order)
        rank = {n: idx for idx, n in enumerate(priority_order)}
        ordered = sorted(
            filtered,
            key=lambda n: (0 if n in rank else (1 if n in first_set else 2), rank.get(n, 0)),
        )

        out: list[str] = []
        used = 0
        omitted: list[str] = []
        decl_only_count = 0
        reduced: list[str] = []
        for name in ordered:
            body = bodies[name]
            if focused and name not in full_set and not (
                len(body) < SMALL_FILE_CHARS
                or not _is_source_code(name)
                or posixpath.dirname(_norm(name)) in focus_dirs
            ):
                decls = _extract_declarations(body)
                header = f"--- {name} (declarations only; non-focus file)"
                entry = f"{header}\n{decls}" if decls else f"{header}\n(no top-level declarations)"
                cost = len(decls)
                if used + cost > cap:
                    omitted.append(name)
                    out.append(f"--- {name} (omitted, size cap)")
                    continue
                used += cost
                decl_only_count += 1
                reduced.append(name)
                out.append(entry)
                continue
            if used + len(body) > cap:
                omitted.append(name)
                out.append(f"--- {name} (omitted, size cap)")
                continue
            used += len(body)
            out.append(f"--- {name}\n{body}")
        if focused and decl_only_count:
            out.insert(
                0,
                "Focused snapshot: full text is shown for task Inputs/Outputs, sibling source files in the same "
                "directories, and files defining helpers they use; other tracked files show declaration lines only "
                "(func, type, var, const, def, class, interface, plus their preceding comment line). "
                "Reduced to declarations: " + ", ".join(reduced),
            )
        if omitted:
            out.insert(0, "Files omitted for size (not shown below): " + ", ".join(omitted))
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
        # Siblings: other tracked source files in the same directories, own 40k budget.
        exc = ctx.get("exclude") if "exclude" in ctx else SNAPSHOT_EXCLUDE_DEFAULT
        prot = self._protected_patterns()  # docs like plan.md are withheld from the reviewer on purpose
        seen, used = set(names), 0
        for d in dict.fromkeys(posixpath.dirname(n) for n in names):
            listing = _git(self.dir, "ls-tree", "-z", branch, *([d + "/"] if d else []), check=False)
            for entry in listing.split("\0"):
                meta, _, sib = entry.partition("\t")
                if not sib or meta.split()[1] != "blob" or sib in seen or _match_any(sib, prot) or (exc and _match_any(sib, exc)):
                    continue
                seen.add(sib)
                try:
                    body = _git(self.dir, "show", f"{branch}:{sib}", check=False)
                except UnicodeDecodeError:
                    continue
                if not body or "\0" in body:
                    continue
                if used + len(body) > SIBLING_CHARS:
                    out.append(f"--- {sib} (omitted, size cap)")
                    continue
                used += len(body)
                out.append(f"--- {sib}\n{body}")
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
        """True only if integration holds a merge commit the orchestrator made for this task
        ("Merge <id>: ..." from merge()). Ancestry alone never counts: a branch with no commits
        is an ancestor of integration, and must not be mistaken for finished work."""
        if not (self.dir / ".git").exists():
            return False
        if not _git(self.dir, "rev-parse", "--verify", "--quiet", self.integ, check=False).strip():
            return False
        out = _git(self.dir, "log", "--merges", "--format=%H", "-1", f"--grep=^Merge {task.id}:", self.integ, check=False)
        return bool(out.strip())

    def _protected_patterns(self) -> list[str]:
        """PROTECTED_DEFAULT plus cfg protected_paths. The config can only add, never replace."""
        extra = [str(p) for p in self.cfg.get("protected_paths") or []]
        return [(_norm(p) + "/*" if p.endswith("/") else _norm(p)).casefold() for p in PROTECTED_DEFAULT + extra]

    def _filter_protected(self, paths: Iterable[str]) -> list[str]:
        pats = self._protected_patterns()
        return [raw for raw in paths if _match_any(raw, pats)]

    def _protected_hits(self, patch: Path) -> list[str]:
        toks = _git_apply(self.dir, "--numstat", "-z", str(patch)).split("\0")
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
        return self._call_role(
            "planner", bounded=False,
            constitution=self.doc_text("constitution"), spec=self.doc_text("spec"),
            plan=self.doc_text("plan"), project_yaml=self._read("project.yaml"),
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
        result = self._call_role(
            "developer",
            bounded=task.bounded,
            constitution=self.doc_text("constitution"),
            spec=self.doc_text("spec"),
            plan=self.doc_text("plan"),
            repository=self._snapshot(task=task),
            output_format="Reply with ONE unified diff in a ```diff fenced block (paths relative to the repo root, "
                          "git apply compatible), then a one paragraph note. No other code blocks.",
            task=self._task_text(task),
            notes_from_previous_attempt=notes,
            **self._rejected_attempt(),
        )
        self.record("developer-reply.md", result.text)
        m = DIFF_BLOCK.search(result.text)
        if not m:
            raise AgentFailed("no ```diff block in the developer reply")
        self.ensure_integration()
        branch = self.task_branch(task)
        patch = self.dir / "logs" / f"{task.id}.patch"
        patch.parent.mkdir(exist_ok=True)
        self.record("diff.patch", m.group(1) + "\n")
        fixed, warns = recount_patch(m.group(1) + "\n")
        patch.write_text(fixed)
        if hits := self._protected_hits(patch):
            raise AgentFailed(f"diff touches protected paths, remove these changes: {', '.join(hits)}")
        with self._worktree(branch, create=True, start=self.integ) as wt:
            used: list[str] = []
            _git_apply(wt, "--index", str(patch), used=used)  # stages exactly the patched files
            warns += [f"git apply needed fallback: {u}" for u in used]
            if warns:
                self.record("patch-warnings.txt", "\n".join(warns) + "\n")
            msg = f"{task.id}: {task.title}" + (f" {result.label}" if result.label else "")
            _git(wt, "commit", "-q", "-m", msg)
        return branch

    def _developer_agent(self, task: Task, notes: str = "") -> str:
        """Run a tool-using coding agent inside a throwaway git worktree, then validate and commit."""
        self.ensure_integration()
        branch = self.task_branch(task)
        cmds = self.cfg.get("commands") or {}
        cmd_hint = "\n".join(f"{k}: {v}" for k, v in cmds.items() if v)
        with self._worktree(branch, create=True, start=self.integ) as wt:
            assert self.agent_runner is not None
            result = self._call_role(
                "developer",
                bounded=task.bounded,
                primary=lambda p: self.agent_runner(p, str(wt)),
                constitution=self.doc_text("constitution"),
                spec=self.doc_text("spec"),
                plan=self.doc_text("plan"),
                commands=cmd_hint,
                instructions="You are working inside an isolated git worktree for this task. Edit files directly using your "
                             "tools. Do NOT run git commit, git checkout, or git merge (the orchestrator commits your "
                             "work). Do NOT touch protected paths. Reply with a short summary of what changed and which "
                             "requirement IDs it satisfies.",
                task=self._task_text(task),
                notes_from_previous_attempt=notes,
                **self._rejected_attempt(),
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

    def _timeout(self) -> float:
        return float((self.cfg.get("limits") or {}).get("command_timeout_seconds") or COMMAND_TIMEOUT_DEFAULT)

    def _run(self, name: str, cmd: str | list[str], cwd: Path, shell: bool) -> tuple[Optional[int], str, str, str]:
        """Run one command in its own session. On timeout the whole process group gets SIGTERM, then
        SIGKILL after 5s, so grandchildren die too. -> (exit code or None on timeout, stdout, stderr,
        timeout message)."""
        secs = self._timeout()
        try:
            p = subprocess.Popen(cmd, shell=shell, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        except OSError as e:  # for example the verify binary is not installed
            return 127, "", str(e), ""
        try:
            out, err = p.communicate(timeout=secs)
            return p.returncode, out, err, ""
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(5)
            with contextlib.suppress(ProcessLookupError):  # also reaps children that ignored SIGTERM
                os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            tail = "\n".join((out + err).splitlines()[-200:])
            shown = cmd if isinstance(cmd, str) else shlex.join(cmd)
            return None, out, err, f"timeout: {name} exceeded {secs:g}s\n$ {shown}\n{tail}"

    def verify_problem(self, task: Task) -> str:
        """Why the task's `Verify:` line may not run (empty string when fine or absent)."""
        if not task.verify:
            return ""
        allowed = self.cfg.get("verify_allowlist") or VERIFY_ALLOWLIST_DEFAULT
        try:
            argv = shlex.split(task.verify)
        except ValueError as e:
            return f"verify command not allowed: cannot parse {task.verify!r} ({e})"
        if not argv or argv[0] not in allowed:
            return f"verify command not allowed: {argv[0] if argv else ''!r} (allowed: {', '.join(allowed)})"
        if ops := [t for t in argv if t in SHELL_OPERATORS]:
            return f"verify command not allowed: shell operator {ops[0]!r} (no shell is used)"
        return ""

    def verify(self, task: Task, branch: str) -> tuple[bool, str]:
        """Before review, in a worktree for `branch`: commands.build, then the task's `Verify:` line
        (no shell, first token must be in verify_allowlist), then commands.test. The full test suite
        is never skipped, so it always gates the merge. With nothing configured this is a pass."""
        if problem := self.verify_problem(task):  # also checked before the developer runs
            return False, problem
        cmds = self.cfg.get("commands") or {}
        steps: list[tuple[str, str | list[str], bool]] = []
        if (cmds.get("build") or "").strip():
            steps.append(("build", cmds["build"].strip(), True))
        if task.verify:
            argv = shlex.split(task.verify)  # verify_problem already vetted it
            steps.append(("task verify", argv, False))
        if (cmds.get("test") or "").strip():
            steps.append(("test", cmds["test"].strip(), True))
        if not steps:
            return True, ""
        logs: list[str] = []
        with self._worktree(branch) as wt:
            for name, cmd, shell in steps:
                rc, out, err, timeout_msg = self._run(name, cmd, wt, shell)
                shown = cmd if isinstance(cmd, str) else shlex.join(cmd)
                logs.append(f"$ {shown}  ({name}, exit {rc if rc is not None else 'timeout'})\n--- stdout\n{out}\n--- stderr\n{err}")
                if rc != 0:
                    self.record("verify.txt", "\n".join(logs))
                    if timeout_msg:
                        return False, timeout_msg
                    return False, f"commands.{name} failed (exit {rc}):\n$ {shown}\n{(out + err)[-2000:]}"
        self.record("verify.txt", "\n".join(logs))
        return True, ""

    def reviewer(self, task: Task, branch: str) -> tuple[bool, str]:
        """Fresh context: constitution, spec, touched files, diff, task. No developer notes."""
        diff = _git(self.dir, "diff", f"{self.integ}...{branch}")
        touched = self._touched_context(branch)
        text = self._call_role(
            "reviewer",
            bounded=False,
            constitution=self.doc_text("constitution"),
            spec=self.doc_text("spec"),
            touched_files=touched,
            diff=diff,
            task=self._task_text(task),
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
            rc, out, err, timeout_msg = self._run("smoke", cmd, wt, True)
        code = rc if rc is not None else "timeout"
        self.record("smoke.txt", f"$ {cmd}\nexit code: {code}\n--- stdout\n{out}\n--- stderr\n{err}")
        ev = self.dir / "logs" / "evidence" / task.id
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "stdout.txt").write_text(out)
        (ev / "stderr.txt").write_text(err)
        (ev / "exit_code.txt").write_text(f"{code}\n")
        if timeout_msg:
            return False, timeout_msg
        return rc == 0, f"smoke exit {rc}; evidence in {ev}\n{(out + err)[-1500:]}"

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

    def _evidence_text(self) -> str:
        """Per task dir under logs/evidence: exit code plus the last 1500 chars of stdout and stderr, inline."""
        ev = self.dir / "logs" / "evidence"
        parts = []
        for d in sorted(p for p in ev.glob("*") if p.is_dir()) if ev.exists() else []:
            def tail(name):
                f = d / name
                return f.read_text(errors="replace")[-1500:] if f.exists() else "(missing)"
            parts.append(f"--- {d.name}\nexit code: {tail('exit_code.txt').strip()}\n"
                         f"stdout (last 1500 chars):\n{tail('stdout.txt')}\nstderr (last 1500 chars):\n{tail('stderr.txt')}")
        return "\n".join(parts)

    def gap_analyzer(self) -> str:
        tasks = self._read("tasks.md")
        outputs = " ".join(t.fields.get("Outputs", "") for t in parse_tasks(tasks))
        tree = _git(self.dir, "ls-tree", "-r", "--name-only", self._tip()).splitlines()
        named = [n for n in tree if n in outputs]  # files a task Outputs line names
        return self._call_role(
            "gap-analyzer",
            bounded=False,
            spec=self.doc_text("spec"),
            plan=self.doc_text("plan"),
            repository=self._snapshot(named),
            tasks=tasks,
            evidence=self._evidence_text(),
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

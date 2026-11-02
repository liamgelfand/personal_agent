"""Workspaces: a repo's main checkout or an agent task's worktree, plus safe file access."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .config import DATA_DIR, WORKTREES_DIR, Access, Config, Repo
from .store import Store

MAX_FILE_BYTES = 2_000_000
MAX_LIST_FILES = 50_000
# Heavy or generated directories skipped when walking (files git tracks in them are still listed).
WALK_EXCLUDES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode-test",
}
# This app's own task worktrees and checkpoint copies, so they don't appear inside its tree.
EXCLUDED_DIRS = {WORKTREES_DIR.resolve(), (DATA_DIR / "checkpoints").resolve()}


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    repo: Repo
    task_id: str | None
    path: Path
    access: Access

    @property
    def key(self) -> str:
        return f"{self.repo.name}@{self.task_id}" if self.task_id else self.repo.name

    def as_root(self) -> Repo:
        """The guard's view of this workspace."""
        return Repo(name=self.key, path=self.path, access=self.access)

    def resolve(self, rel: str) -> Path:
        target = (self.path / rel).resolve()
        if target != self.path and self.path not in target.parents:
            raise WorkspaceError(f"{rel} is outside the workspace")
        return target

    def require_write(self) -> None:
        if self.access is not Access.WRITE:
            raise WorkspaceError(f"'{self.repo.name}' is read-only - set access: write in config.yaml")


def get_workspace(config: Config, store: Store, repo: str, task: str | None = None) -> Workspace:
    try:
        repo_cfg = config.repo(repo)
    except KeyError as exc:
        raise WorkspaceError(str(exc)) from exc
    if not task:
        return Workspace(repo_cfg, None, repo_cfg.path, repo_cfg.access)
    row = store.task(task)
    if not row or row["repo"] != repo or row["status"] in ("discarded", "merged"):  # worktree is gone
        raise WorkspaceError(f"unknown task: {task}")
    # A task's worktree is its own sandbox, so it is always writable.
    return Workspace(repo_cfg, task, Path(row["worktree"]), Access.WRITE)


async def list_files(ws: Workspace) -> tuple[list[str], bool]:
    """Every file on disk, git-ignored ones too, as sorted POSIX paths relative to the workspace,
    plus whether the list was cut at MAX_LIST_FILES. Files git doesn't ignore are always kept;
    ignored files fill the remaining budget. WALK_EXCLUDES dirs are walked past but tracked files in them stay."""
    visible, ignored, truncated = await _listing(ws.path)
    return sorted(visible + ignored), truncated


async def _listing(root: Path) -> tuple[list[str], list[str], bool]:
    """(files git doesn't ignore, other files found on disk, whether the list was cut), each sorted."""
    visible = await _git_visible_files(root)
    if len(visible) > MAX_LIST_FILES:
        return visible[:MAX_LIST_FILES], [], True
    ignored, truncated = await asyncio.to_thread(_walk, root, set(visible), MAX_LIST_FILES - len(visible))
    return visible, ignored, truncated


async def _git_visible_files(root: Path) -> list[str]:
    """Tracked files plus untracked-but-not-ignored ones outside WALK_EXCLUDES dirs, existing on disk.
    Empty outside a git repo."""
    if not await gitops.is_repo(root):
        return []
    tracked = await gitops.git(root, "ls-files", "-z", "--cached")
    untracked = await gitops.git(root, "ls-files", "-z", "--others", "--exclude-standard")
    files = {p for p in tracked.split("\0") if p}
    files |= {p for p in untracked.split("\0") if p and not WALK_EXCLUDES.intersection(p.split("/")[:-1])}
    return sorted(p for p in files if (root / p).is_file())


def _walk(root: Path, known: set[str], budget: int) -> tuple[list[str], bool]:
    """Files under root not in `known`, up to `budget`, in sorted order (so a cut is deterministic).
    Skips excluded and symlinked dirs and `.git` link files (worktrees, submodules)."""
    excluded_names = {d.name for d in EXCLUDED_DIRS}
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):  # does not follow symlinked directories
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in WALK_EXCLUDES
            and not (d in excluded_names and (Path(dirpath) / d).resolve() in EXCLUDED_DIRS)
        )
        rel = Path(dirpath).relative_to(root)
        for f in sorted(filenames):
            p = (rel / f).as_posix()
            if f == ".git" or p in known:
                continue
            files.append(p)
            if len(files) > budget:
                return files[:budget], True
    return files, False


SEARCH_LIMIT = 500


async def search(ws: Workspace, query: str, regex: bool = False, case: bool = False) -> dict:
    """Text search (Python regex syntax) across every listed file, git-ignored ones too.
    Returns up to SEARCH_LIMIT matches, files git doesn't ignore first. Raises ValueError for a bad regex."""
    if not query:
        return {"matches": [], "truncated": False}
    try:
        pattern = re.compile(query if regex else re.escape(query), 0 if case else re.IGNORECASE)
    except re.error as exc:
        raise ValueError(str(exc)) from exc
    visible, ignored, truncated = await _listing(ws.path)
    matches = await asyncio.to_thread(_search, ws.path, visible + ignored, pattern, not regex)
    return {"matches": matches[:SEARCH_LIMIT], "truncated": truncated or len(matches) > SEARCH_LIMIT}


def _search(root: Path, files: list[str], pattern: re.Pattern, literal: bool) -> list[dict]:
    """Up to SEARCH_LIMIT + 1 matches; skips binary, large, and files that resolve outside root (symlinks)."""
    root = root.resolve()
    matches = []
    for rel in files:
        try:
            path = (root / rel).resolve()
            if root not in path.parents or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            with path.open("rb") as fh:
                head = fh.read(8000)
                if b"\0" in head:  # binary: don't read the rest
                    continue
                data = head + fh.read()
        except OSError:
            continue
        text = data.decode("utf-8", errors="ignore")
        if literal and not pattern.search(text):  # a literal can't span lines, so this skip is exact
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                matches.append({"path": rel, "line": n, "text": line[:300]})
                if len(matches) > SEARCH_LIMIT:
                    return matches
    return matches


def read_file(ws: Workspace, rel: str) -> dict:
    path = ws.resolve(rel)
    if not path.is_file():
        raise WorkspaceError(f"not a file: {rel}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return {"path": rel, "binary": False, "too_large": True, "size": size, "content": ""}
    data = path.read_bytes()
    if b"\0" in data[:8000]:
        return {"path": rel, "binary": True, "too_large": False, "size": size, "content": ""}
    return {"path": rel, "binary": False, "too_large": False, "size": size, "content": data.decode("utf-8", errors="replace")}


def write_file(ws: Workspace, rel: str, content: str) -> None:
    ws.require_write()
    path = ws.resolve(rel)
    if ".git" in path.relative_to(ws.path).parts:  # a worktree's .git link file or git internals
        raise WorkspaceError(f"{rel} is git metadata")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file's existing line endings so saving doesn't rewrite every line.
    if path.is_file() and b"\r\n" in path.read_bytes()[:8000]:
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(content.encode("utf-8"))

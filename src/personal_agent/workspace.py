"""Workspaces: a repo's main checkout or an agent task's worktree, plus safe file access."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
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
    files, _, truncated = await list_entries(ws)
    return files, truncated


async def list_entries(ws: Workspace) -> tuple[list[str], list[str], bool]:
    """list_files plus the empty directories found on the way, which a file list alone can't show."""
    visible, ignored, empty_dirs, truncated = await _listing(ws.path)
    return sorted(visible + ignored), empty_dirs, truncated


async def _listing(root: Path) -> tuple[list[str], list[str], list[str], bool]:
    """(files git doesn't ignore, other files found on disk, empty dirs, whether the list was cut), each sorted."""
    visible = await _git_visible_files(root)
    if len(visible) > MAX_LIST_FILES:
        return visible[:MAX_LIST_FILES], [], [], True
    ignored, empty_dirs, truncated = await asyncio.to_thread(_walk, root, set(visible), MAX_LIST_FILES - len(visible))
    return visible, ignored, empty_dirs, truncated


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


def _is_empty(path: Path) -> bool:
    try:
        return not any(path.iterdir())
    except OSError:
        return False


def _walk(root: Path, known: set[str], budget: int) -> tuple[list[str], list[str], bool]:
    """Files under root not in `known`, up to `budget`, in sorted order (so a cut is deterministic),
    and the empty directories passed. Skips excluded and symlinked dirs and `.git` link files (worktrees, submodules)."""
    excluded_names = {d.name for d in EXCLUDED_DIRS}
    files: list[str] = []
    empty_dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):  # does not follow symlinked directories
        if not dirnames and not filenames and Path(dirpath) != root:
            empty_dirs.append(Path(dirpath).relative_to(root).as_posix())
        for d in dirnames:  # skipped dirs can't be listed, but a fresh empty one should still show
            sub = Path(dirpath) / d
            if d in WALK_EXCLUDES and not sub.is_symlink() and _is_empty(sub):
                empty_dirs.append(sub.relative_to(root).as_posix())
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
                return files[:budget], empty_dirs, True
    return files, empty_dirs, False


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
    visible, ignored, _, truncated = await _listing(ws.path)
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
    if _is_git_metadata(ws, path):  # a worktree's .git link file or git internals
        raise WorkspaceError(f"{rel} is git metadata")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file's existing line endings so saving doesn't rewrite every line.
    if path.is_file() and b"\r\n" in path.read_bytes()[:8000]:
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(content.encode("utf-8"))


def _entry(ws: Workspace, rel: str) -> Path:
    """rel with its parent resolved but not the entry itself, so a symlink is moved or deleted rather than its target.
    Refuses the workspace root and git metadata."""
    name = Path(rel).name
    if name in ("", ".", ".."):
        raise WorkspaceError(f"invalid path: {rel}")
    path = ws.resolve(str(Path(rel).parent)) / name
    if _is_git_metadata(ws, path):
        raise WorkspaceError(f"{rel} is git metadata")
    return path


def _is_git_metadata(ws: Workspace, path: Path) -> bool:
    """Whether path is or is inside a `.git` entry. Names are compared the way Windows reads them (any case,
    trailing dots and spaces dropped), and an existing entry is checked under its real name, which also
    catches 8.3 short names like GIT~1."""
    if path.exists() and not path.is_symlink():
        real = path.resolve()
        if real.parent == path.parent:  # just the real spelling of the name, not a junction elsewhere
            path = real
    return any(part.rstrip(". ").lower() == ".git" for part in path.relative_to(ws.path).parts)


def create_entry(ws: Workspace, rel: str, is_dir: bool) -> None:
    """Make an empty file or directory; never replaces anything already there."""
    ws.require_write()
    path = _entry(ws, rel)
    if path.exists() or path.is_symlink():
        raise WorkspaceError(f"{rel} already exists")
    try:
        if is_dir:
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.open("xb").close()
    except OSError as exc:
        raise WorkspaceError(f"could not create {rel}: {exc.strerror or exc}") from exc


def rename_entry(ws: Workspace, rel: str, new_rel: str) -> None:
    """Move a file or directory; refuses to overwrite, except a case-only rename of the same entry."""
    ws.require_write()
    src, dst = _entry(ws, rel), _entry(ws, new_rel)
    if not (src.exists() or src.is_symlink()):
        raise WorkspaceError(f"not found: {rel}")
    if (dst.exists() or dst.is_symlink()) and not (dst.exists() and src.exists() and dst.samefile(src)):
        raise WorkspaceError(f"{new_rel} already exists")
    if src.is_dir() and not src.is_symlink() and src in dst.parents:
        raise WorkspaceError(f"can't move {rel} into itself")
    made = [p for p in dst.parents if ws.path in p.parents and not p.exists()]  # deepest first
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    except OSError as exc:
        for p in made:  # don't leave the new location's folders behind
            with contextlib.suppress(OSError):
                p.rmdir()
        raise WorkspaceError(f"could not rename {rel}: {exc.strerror or exc}") from exc


def _is_junction(path: Path) -> bool:
    """A Windows directory junction, which is_symlink() misses and rmtree refuses."""
    if os.name != "nt" or path.is_symlink():
        return False
    try:
        return bool(path.lstat().st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def delete_entry(ws: Workspace, rel: str) -> None:
    """Delete a file, symlink, or directory with everything in it."""
    ws.require_write()
    path = _entry(ws, rel)
    try:
        if _is_junction(path):
            path.rmdir()  # remove the link, not what it points to
        elif path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            raise WorkspaceError(f"not found: {rel}")
    except OSError as exc:
        raise WorkspaceError(f"could not delete {rel}: {exc.strerror or exc}") from exc

"""Workspaces: a repo's main checkout or an agent task's worktree, plus safe file access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .config import Access, Config, Repo
from .store import Store

MAX_FILE_BYTES = 2_000_000
WALK_EXCLUDES = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next"}


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


async def list_files(ws: Workspace) -> list[str]:
    """All non-ignored files, as POSIX paths relative to the workspace."""
    if await gitops.is_repo(ws.path):
        out = await gitops.git(ws.path, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        files = {p for p in out.split("\0") if p}
        return sorted(p for p in files if (ws.path / p).is_file())
    files = []
    for dirpath, dirnames, filenames in os.walk(ws.path):
        dirnames[:] = [d for d in dirnames if d not in WALK_EXCLUDES]
        rel = Path(dirpath).relative_to(ws.path)
        files += [(rel / f).as_posix() for f in filenames]
    return sorted(files)


SEARCH_LIMIT = 500


async def search(ws: Workspace, query: str, regex: bool = False, case: bool = False) -> dict:
    """Text search across the workspace's non-ignored files. Returns up to SEARCH_LIMIT matches."""
    if not query:
        return {"matches": [], "truncated": False}
    if await gitops.is_repo(ws.path):
        args = ["grep", "-n", "-I", "-z", "--untracked", "--full-name", "-E" if regex else "-F"]
        if not case:
            args.append("-i")
        out = await gitops.git(ws.path, *args, "-e", query, check=False)
        matches = []
        for line in out.splitlines():
            parts = line.split("\0", 2)  # path NUL line NUL text
            if len(parts) == 3 and parts[1].isdigit():
                matches.append({"path": parts[0], "line": int(parts[1]), "text": parts[2][:300]})
                if len(matches) > SEARCH_LIMIT:
                    break
        return {"matches": matches[:SEARCH_LIMIT], "truncated": len(matches) > SEARCH_LIMIT}
    import re

    pattern = re.compile(query if regex else re.escape(query), 0 if case else re.IGNORECASE)
    matches = []
    for rel in await list_files(ws):
        path = ws.path / rel
        if path.stat().st_size > MAX_FILE_BYTES:
            continue
        for n, text in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if pattern.search(text):
                matches.append({"path": rel, "line": n, "text": text[:300]})
                if len(matches) > SEARCH_LIMIT:
                    return {"matches": matches[:SEARCH_LIMIT], "truncated": True}
    return {"matches": matches, "truncated": False}


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
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file's existing line endings so saving doesn't rewrite every line.
    if path.is_file() and b"\r\n" in path.read_bytes()[:8000]:
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    path.write_bytes(content.encode("utf-8"))

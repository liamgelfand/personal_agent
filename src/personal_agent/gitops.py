"""Async git helpers: status, diffs, staging, commits, and worktrees."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any


class GitError(RuntimeError):
    pass


async def git(root: Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise GitError(err.decode(errors="replace").strip() or f"git {' '.join(args)} failed")
    return out.decode(errors="replace")


async def is_repo(root: Path) -> bool:
    return (await git(root, "rev-parse", "--is-inside-work-tree", check=False)).strip() == "true"


async def status(root: Path) -> dict[str, Any]:
    """Branch info plus one entry per changed path, split into staged/unstaged like VS Code."""
    raw = await git(root, "status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all")
    entries = raw.split("\0")
    branch = entries[0][3:] if entries and entries[0].startswith("## ") else ""
    files: list[dict[str, Any]] = []
    i = 1
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        orig = None
        if x in "RC":
            orig = entries[i] if i < len(entries) else None  # with -z the source path follows
            i += 1
        files.append({
            "path": path,
            "orig": orig,
            "index": x,
            "worktree": y,
            "untracked": x == "?",
            "staged": x not in " ?",
            "unstaged": y != " " or x == "?",
        })
    return {"branch": branch, "files": files}


async def show(root: Path, ref_path: str) -> str | None:
    """File contents at a ref, e.g. 'HEAD:src/a.py' or ':src/a.py' (index); None if absent."""
    if not await _exists(root, ref_path):
        return None
    return await git(root, "show", ref_path)


async def _exists(root: Path, ref_path: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "cat-file", "-e", ref_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def diff_sides(root: Path, path: str, staged: bool) -> dict[str, str]:
    """Original/modified text for a side-by-side diff.

    staged:   HEAD   -> index
    unstaged: index  -> working tree (index falls back to HEAD, then empty for new files)
    """
    head = await show(root, f"HEAD:{path}")
    index = await show(root, f":{path}")
    if staged:
        return {"original": head or "", "modified": index or ""}
    file = root / path
    modified = file.read_text(encoding="utf-8", errors="replace") if file.is_file() else ""
    original = index if index is not None else (head or "")
    return {"original": original, "modified": modified}


async def stage(root: Path, paths: list[str]) -> None:
    await git(root, "add", "-A", "--", *paths)


async def unstage(root: Path, paths: list[str]) -> None:
    if await _exists(root, "HEAD"):
        await git(root, "restore", "--staged", "--", *paths)
    else:
        await git(root, "rm", "--cached", "-r", "--quiet", "--", *paths)


def _remove_new_file(root: Path, rel: str) -> None:
    """Delete a file that is new in the working tree. A symlink is removed itself, never its target."""
    target = root / rel
    if root.resolve() in target.parent.resolve().parents or target.parent.resolve() == root.resolve():
        if target.is_symlink() or target.is_file():
            target.unlink()


async def discard(root: Path, paths: list[str]) -> None:
    """Throw away changes: restore tracked files from HEAD, delete untracked or added ones."""
    st = {f["path"]: f for f in (await status(root))["files"]}
    restore: list[str] = []
    remove: list[str] = []
    for p in paths:
        entry = st.get(p)
        if not entry:
            continue
        if entry["index"] == "R" and entry["orig"]:
            restore.append(entry["orig"])  # bring back the old name, drop the new one
            remove.append(p)
        elif entry["untracked"] or entry["index"] in "AC":
            remove.append(p)
        else:
            restore.append(p)
    for p in remove:
        # --force: git otherwise refuses when the staged copy differs from both HEAD and the file.
        await git(root, "rm", "--cached", "--force", "--quiet", "--ignore-unmatch", "--", p)
        _remove_new_file(root, p)
    if restore:
        await git(root, "restore", "--staged", "--worktree", "--source=HEAD", "--", *restore)


async def commit(root: Path, message: str) -> str:
    await git(root, "commit", "-m", message)
    return (await git(root, "rev-parse", "--short", "HEAD")).strip()


async def staged_diff(root: Path) -> str:
    return await git(root, "diff", "--cached")


async def has_commits(root: Path) -> bool:
    return await _exists(root, "HEAD")


async def head_sha(root: Path) -> str:
    return (await git(root, "rev-parse", "HEAD")).strip()


async def add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")


async def remove_worktree(repo: Path, path: Path, branch: str) -> None:
    await git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    await git(repo, "worktree", "prune", check=False)
    await git(repo, "branch", "-D", branch, check=False)

"""Async git helpers: status, diffs, staging, commits, worktrees, and remotes."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote


class GitError(RuntimeError):
    pass


# For commands that talk to a remote in the background: fail instead of asking for credentials.
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


async def git(root: Path, *args: str, check: bool = True, env: dict[str, str] | None = None,
              timeout: float | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env} if env else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {' '.join(args)} timed out") from None
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


async def default_branch(repo: Path) -> str:
    """The branch PRs target: origin's HEAD branch.

    refs/remotes/origin/HEAD only exists in clones, so a repo that was `git init`ed and pushed asks
    origin itself. Failing that (offline, say), a pushed main or master; then 'main'.
    """
    prefix = "refs/remotes/origin/"
    ref = (await git(repo, "symbolic-ref", "--quiet", f"{prefix}HEAD", check=False)).strip()
    if ref.startswith(prefix):
        return ref.removeprefix(prefix)
    try:
        out = await git(repo, "ls-remote", "--symref", "origin", "HEAD", env=NO_PROMPT, timeout=20)
    except GitError:
        out = ""
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
    for name in ("main", "master"):
        if await _exists(repo, f"{prefix}{name}"):
            return name
    return "main"


async def remote_url(root: Path, remote: str = "origin") -> str | None:
    return (await git(root, "remote", "get-url", remote, check=False)).strip() or None


_GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://(?:[^@/]+@)?github\.com(?::\d+)?/|[^@/:]+@github\.com:)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def github_repo(url: str) -> tuple[str, str] | None:
    """(owner, repo) of a GitHub https or ssh remote URL; None for anything else."""
    match = _GITHUB_REMOTE.match(url.strip())
    return (match[1], match[2]) if match else None


def compare_url(owner: str, repo: str, base: str, branch: str, title: str) -> str:
    """GitHub's page for opening a PR of `branch` into `base` by hand."""
    return (
        f"https://github.com/{owner}/{repo}/compare/{quote(base)}...{quote(branch)}"
        f"?expand=1&title={quote(title, safe='')}"
    )


async def add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")


def is_link(path: Path) -> bool:
    """A symlink or, on Windows, a directory junction (which Python 3.11's is_symlink misses)."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def package_dirs(root: Path) -> list[Path]:
    """The root and its direct subfolders that hold a package.json (not following links)."""
    subdirs = sorted(d for d in root.iterdir() if d.is_dir() and not is_link(d)) if root.is_dir() else []
    return [d for d in [root, *subdirs] if (d / "package.json").is_file()]


def dependency_dirs(repo: Path) -> list[str]:
    """node_modules folders in a checkout, at the top level or one down, each next to a package.json."""
    return [
        (d / "node_modules").relative_to(repo).as_posix() for d in package_dirs(repo)
        if (d / "node_modules").is_dir() and not is_link(d / "node_modules")
    ]


def dependency_links(worktree: Path) -> list[str]:
    """node_modules links in a worktree (see link_dependencies)."""
    dirs = [worktree, *(d for d in worktree.iterdir() if d.is_dir() and not is_link(d))] if worktree.is_dir() else []
    return [(d / "node_modules").relative_to(worktree).as_posix() for d in dirs if is_link(d / "node_modules")]


def _link_dir(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi  # a junction needs no admin rights or developer mode, unlike a symlink

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _unlink_dir(link: Path) -> None:
    """Remove a directory link itself, never what it points to."""
    if os.name == "nt":
        os.rmdir(link)  # removes a junction or directory symlink without touching the target
    else:
        os.unlink(link)


async def link_dependencies(repo: Path, worktree: Path) -> list[str]:
    """Link the main checkout's node_modules folders into a new worktree; returns the paths linked.

    Skips folders the worktree already has and packages it lacks. A link git does not already
    ignore is added to the repo's info/exclude, so it is never committed.
    """
    linked: list[str] = []
    for rel in dependency_dirs(repo):
        link = worktree / rel
        if link.exists() or is_link(link) or not (link.parent / "package.json").is_file():
            continue
        _link_dir(link, repo / rel)
        linked.append(rel)
        if not (await git(worktree, "check-ignore", "--", rel, check=False)).strip():
            exclude = Path((await git(worktree, "rev-parse", "--git-path", "info/exclude")).strip())
            exclude = exclude if exclude.is_absolute() else worktree / exclude
            exclude.parent.mkdir(parents=True, exist_ok=True)
            lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
            if f"/{rel}" not in lines:
                exclude.write_text("\n".join([*lines, f"/{rel}"]) + "\n", encoding="utf-8")
    return linked


def remove_dependency_links(worktree: Path) -> None:
    for rel in dependency_links(worktree):
        _unlink_dir(worktree / rel)


async def remove_worktree(repo: Path, path: Path, branch: str) -> None:
    # Unlink shared node_modules first, so neither git nor rmtree can reach into the main checkout's copy.
    remove_dependency_links(path)
    await git(repo, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    await git(repo, "worktree", "prune", check=False)
    await git(repo, "branch", "-D", branch, check=False)

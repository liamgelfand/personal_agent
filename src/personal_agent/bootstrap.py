"""Task worktree preparation: per-repo setup commands, and the Environment section the agents get.

Shared node_modules links are made by gitops.link_dependencies; this module runs the repo's
`setup` commands and describes the prepared worktree, so agents don't spend turns rediscovering it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from . import gitops
from .config import Repo

SETUP_TIMEOUT = 600  # seconds per setup command

# Bash commands that install into or delete node_modules. Through a shared link they would change
# the main checkout's copy (and every other task's), so the link is swapped out first.
DEPENDENCY_WRITE = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)(?:\s+--?\S+(?:\s+[^-\s]\S*)?)*?\s+"
    r"(?:install|i|ci|add|uninstall|un|remove|rm|update|up|upgrade|prune|dedupe|rebuild|link)\b"
    r"|\byarn\s*(?:$|[;&|)])"  # bare `yarn` installs
    r"|\b(?:rm|rmdir|rimraf|del)\b[^;&|\n]*node_modules"
)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a shell and everything it started (npm, uv, ...), not just the shell."""
    if proc.returncode is None:
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                os.killpg(proc.pid, signal.SIGKILL)  # its own session, see run_setup
        except OSError:
            proc.kill()
    await proc.wait()


async def run_setup(commands: list[str], cwd: Path) -> list[str]:
    """Run each shell command in `cwd`; returns a message per failure (none raise).

    A timed-out command is killed with its children; so is a running one when the task is
    stopped, discarded or the server shuts down, so nothing keeps writing into the worktree.
    """
    failures: list[str] = []
    for command in commands:
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                **({} if os.name == "nt" else {"start_new_session": True}),
            )
        except OSError as exc:
            failures.append(f"`{command}` could not start: {exc}")
            continue
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), SETUP_TIMEOUT)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            failures.append(f"`{command}` timed out after {SETUP_TIMEOUT}s")
            continue
        except BaseException:
            await asyncio.shield(_kill_tree(proc))
            raise
        if proc.returncode:
            tail = out.decode(errors="replace").strip()[-500:]
            failures.append(f"`{command}` exited with {proc.returncode}" + (f": {tail}" if tail else ""))
    return failures


def dependency_link_hook(worktree: Path, on_unlink: Callable[[list[str], str], Awaitable[None]]) -> HookMatcher:
    """Before a Bash command that would install into or delete node_modules, remove the shared links.

    The command then works on the worktree's own (new) node_modules instead of the main checkout's.
    The command itself is not blocked; the guard hook still decides on it.
    """
    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext):
        command = str(input_data.get("tool_input", {}).get("command", ""))
        if DEPENDENCY_WRITE.search(command) and (links := gitops.dependency_links(worktree)):
            gitops.remove_dependency_links(worktree)
            await on_unlink(links, command)
        return {}

    return HookMatcher(matcher="Bash", hooks=[hook])


def _detect_verify(repo: Repo, worktree: Path) -> list[str]:
    """Conservative guesses: pytest through the main checkout's venv, and each package's type check."""
    commands: list[str] = []
    python = repo.path / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if (worktree / "tests").is_dir() and python.is_file():
        # The venv's editable install points at the main checkout; PYTHONPATH makes it import this one.
        env = f"PYTHONPATH={shlex.quote((worktree / 'src').as_posix())} " if (worktree / "src").is_dir() else ""
        commands.append(f"{env}{shlex.quote(python.as_posix())} -m pytest tests")
    for pkg in gitops.package_dirs(worktree):
        try:
            scripts = json.loads((pkg / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError, AttributeError):
            continue
        cd = "" if pkg == worktree else f"cd {shlex.quote(pkg.name)} && "
        tsconfig = pkg / "tsconfig.json"
        if "typecheck" in scripts:
            commands.append(f"{cd}npm run typecheck")
        elif tsconfig.is_file() and '"references"' not in tsconfig.read_text(encoding="utf-8", errors="replace"):
            commands.append(f"{cd}npx tsc --noEmit")
    return commands


def environment_section(repo: Repo, worktree: Path, role: str) -> str:
    """The Environment section appended to the implementer's or reviewer's system prompt for a task."""
    lines = ["## Environment", "", "The pipeline prepared this worktree; do not spend turns re-discovering the environment."]
    if links := gitops.dependency_links(worktree):
        listed = ", ".join(f"`{rel}`" for rel in links)
        lines.append(f"- Linked from the main checkout, already installed (do not reinstall or delete): {listed}")
        lines.append(
            "  To add, remove or update a dependency, just run the package manager's install in the worktree "
            "(e.g. `cd frontend && npm install <pkg>`): the pipeline first removes the link, so the install "
            "creates this worktree's own node_modules (a full install, so it takes a while)."
        )
    if repo.setup:
        lines.append("- Setup commands already run: " + ", ".join(f"`{c}`" for c in repo.setup))
    commands = repo.verify or _detect_verify(repo, worktree)
    if commands:
        lines.append("- Verify with (Bash, from the worktree root):")
        lines += [f"  - `{c}`" for c in commands]
    lines.append("")
    if role == "reviewer":
        lines.append(
            "The implementer already ran the tests and type check. Do not rerun the full test suite or "
            "build the frontend; trust the reported results unless something looks wrong, and then run "
            "only the specific test that would show it."
        )
    else:
        if gitops.package_dirs(worktree):
            lines.append("Do not build the full frontend bundle; run the type check instead.")
        lines.append("Run only the relevant tests while working, and the full suite once at the end.")
    return "\n".join(lines)

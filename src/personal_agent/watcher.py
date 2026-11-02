"""Watches delegated repos and task worktrees, telling the UI which workspace changed.

Also notices edits to this app's own source, so the UI can offer a rebuild/restart.
"""

from __future__ import annotations

from pathlib import Path

from watchfiles import Change, DefaultFilter, awatch

from .admin import Admin, classify
from .bus import Bus
from .config import DATA_DIR, PROJECT_ROOT, WORKTREES_DIR, Config


class _Filter(DefaultFilter):
    """Default ignores (.git, node_modules, .venv, ...) plus our own data dir and build output, except worktrees."""

    def __call__(self, change: Change, path: str) -> bool:
        p = Path(path)
        if DATA_DIR in p.parents and WORKTREES_DIR not in p.parents:
            return False
        if "dist" in p.parts:
            return False
        return super().__call__(change, path)


async def watch(config: Config, bus: Bus, admin: Admin) -> None:
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    paths = {r.path for r in config.repos} | {WORKTREES_DIR, PROJECT_ROOT}
    # Drop paths nested in another watched path; watching both reports changes twice.
    roots = [p for p in paths if not any(o != p and o in p.parents for o in paths)]
    async for changes in awatch(*roots, watch_filter=_Filter(), debounce=600, step=100):
        changed: set[tuple[str, str | None]] = set()
        for _, raw in changes:
            p = Path(raw)
            if WORKTREES_DIR in p.parents:
                parts = p.relative_to(WORKTREES_DIR).parts
                if len(parts) >= 2:
                    changed.add((parts[0], parts[1]))
                continue
            if part := classify(p):
                await admin.mark_changed(part)
            for repo in config.repos:
                if repo.path in p.parents:
                    changed.add((repo.name, None))
                    break
        for repo, task in changed:
            await bus.publish({"type": "fs_changed", "repo": repo, "task": task})

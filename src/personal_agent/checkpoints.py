"""Per-turn checkpoints: the original contents of every file an agent edits, so a turn can be undone.

Captured through the file tools (Write/Edit/MultiEdit/NotebookEdit). Changes an agent makes
through Bash are not captured.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

from .config import DATA_DIR
from .store import Store

CHECKPOINT_DIR = DATA_DIR / "checkpoints"
FILE_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    files TEXT NOT NULL,       -- JSON [{path, blob}] ; blob NULL means the file did not exist
    created_at REAL NOT NULL,
    restored_at REAL
);
"""


class CheckpointError(Exception):
    pass


def _target(tool_input: dict[str, Any], cwd: Path) -> Path | None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        return None
    path = Path(raw)
    return (path if path.is_absolute() else cwd / path).resolve()


class Checkpoint:
    """Collects originals during one agent turn."""

    def __init__(self, cwd: Path):
        self.id = uuid.uuid4().hex
        self.cwd = cwd
        self.originals: dict[Path, bytes | None] = {}
        self._pending: dict[str, tuple[Path, bytes | None]] = {}

    def hooks(self) -> dict[str, list[HookMatcher]]:
        async def before(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext):
            path = _target(input_data["tool_input"], self.cwd)
            if path and tool_use_id and path not in self.originals:
                self._pending[tool_use_id] = (path, path.read_bytes() if path.is_file() else None)
            return {}

        async def after(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext):
            # Only edits that actually ran count, so denied calls never become undo targets.
            if tool_use_id and (entry := self._pending.pop(tool_use_id, None)):
                self.originals.setdefault(*entry)
            return {}

        return {
            "PreToolUse": [HookMatcher(matcher=FILE_TOOLS, hooks=[before])],
            "PostToolUse": [HookMatcher(matcher=FILE_TOOLS, hooks=[after])],
        }


class Checkpoints:
    def __init__(self, store: Store):
        self.db = store.db
        self.db.executescript(SCHEMA)
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: Checkpoint, conversation_id: str) -> dict[str, Any] | None:
        if not checkpoint.originals:
            return None
        folder = CHECKPOINT_DIR / checkpoint.id
        folder.mkdir(parents=True)
        files = []
        for i, (path, original) in enumerate(checkpoint.originals.items()):
            blob = None
            if original is not None:
                blob = f"{i}.bin"
                (folder / blob).write_bytes(original)
            files.append({"path": str(path), "blob": blob})
        with self.db:
            self.db.execute(
                "INSERT INTO checkpoints (id, conversation_id, files, created_at) VALUES (?, ?, ?, ?)",
                (checkpoint.id, conversation_id, json.dumps(files), time.time()),
            )
        return {"id": checkpoint.id, "files": [f["path"] for f in files]}

    def restore(self, checkpoint_id: str, allowed_roots: list[Path]) -> list[str]:
        row = self.db.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        if not row:
            raise CheckpointError("checkpoint not found")
        if row["restored_at"]:
            raise CheckpointError("this turn was already undone")
        restored = []
        for f in json.loads(row["files"]):
            path = Path(f["path"])
            if not any(path == r or r in path.parents for r in allowed_roots):
                continue  # repo no longer delegated: leave it alone
            if f["blob"] is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((CHECKPOINT_DIR / checkpoint_id / f["blob"]).read_bytes())
            restored.append(str(path))
        with self.db:
            self.db.execute("UPDATE checkpoints SET restored_at = ? WHERE id = ?", (time.time(), checkpoint_id))
        return restored

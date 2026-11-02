"""SQLite persistence: conversations, their event history, agent tasks, reviews, and run costs."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR

DB_PATH = DATA_DIR / "agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    repo TEXT,                 -- NULL: not linked to a repo
    mode TEXT NOT NULL DEFAULT 'agent',  -- 'agent' (can change code) or 'chat' (discussion only)
    session_id TEXT,           -- Claude session to resume; cleared when the repo changes
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    task_id TEXT,
    ts REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_conversation ON events (conversation_id, id);
CREATE INDEX IF NOT EXISTS events_task ON events (task_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    worktree TEXT NOT NULL,
    status TEXT NOT NULL,      -- queued, implementing, reviewing, ready, failed, stopped, discarded
    review_rounds INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    root TEXT PRIMARY KEY,     -- "repo" or "repo@task"
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,        -- chat, task, review, title
    ref TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    num_turns INTEGER,
    cost_usd REAL,
    is_error INTEGER
);
"""


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(conversations)")}
        if "mode" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'agent'")

    # conversations

    def create_conversation(self, repo: str | None, mode: str = "agent", title: str = "New chat") -> dict[str, Any]:
        now = time.time()
        conv_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO conversations (id, title, repo, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, title, repo, mode, now, now),
            )
        return self.conversation(conv_id)

    def conversation(self, conv_id: str) -> dict[str, Any] | None:
        return _row(self.db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone())

    def conversations(self, repo: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM conversations WHERE 1=1", []
        if repo == "":
            sql += " AND repo IS NULL"
        elif repo:
            sql += " AND repo = ?"
            args.append(repo)
        if query:
            sql += " AND (title LIKE ? OR id IN (SELECT conversation_id FROM events WHERE payload LIKE ?))"
            args += [f"%{query}%", f"%{query}%"]
        rows = self.db.execute(sql + " ORDER BY updated_at DESC LIMIT 200", args).fetchall()
        return [dict(r) for r in rows]

    def update_conversation(self, conv_id: str, **fields: Any) -> dict[str, Any] | None:
        if fields:
            fields["updated_at"] = time.time()
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self.db:
                self.db.execute(f"UPDATE conversations SET {cols} WHERE id = ?", (*fields.values(), conv_id))
        return self.conversation(conv_id)

    def delete_conversation(self, conv_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM events WHERE conversation_id = ?", (conv_id,))
            self.db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))

    # events

    def add_event(self, payload: dict[str, Any], conversation_id: str | None = None, task_id: str | None = None) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO events (conversation_id, task_id, ts, payload) VALUES (?, ?, ?, ?)",
                (conversation_id, task_id, time.time(), json.dumps(payload)),
            )
            if conversation_id:
                self.db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?", (time.time(), conversation_id)
                )
        return cur.lastrowid

    def update_event(self, event_id: int, payload: dict[str, Any]) -> None:
        with self.db:
            self.db.execute("UPDATE events SET payload = ? WHERE id = ?", (json.dumps(payload), event_id))

    def event_rows(self, conversation_id: str) -> list[tuple[int, dict[str, Any]]]:
        """A conversation's events with their row ids, oldest first."""
        rows = self.db.execute(
            "SELECT id, payload FROM events WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        )
        return [(r["id"], json.loads(r["payload"])) for r in rows]

    def delete_events_from(self, conversation_id: str, event_id: int) -> None:
        """Delete a conversation's events from event_id (inclusive) onward."""
        with self.db:
            self.db.execute("DELETE FROM events WHERE conversation_id = ? AND id >= ?", (conversation_id, event_id))

    def events(self, conversation_id: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        if conversation_id:
            rows = self.db.execute(
                "SELECT payload FROM events WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            )
        else:
            rows = self.db.execute("SELECT payload FROM events WHERE task_id = ? ORDER BY id", (task_id,))
        return [json.loads(r["payload"]) for r in rows]

    # tasks

    def create_task(self, **fields: Any) -> dict[str, Any]:
        now = time.time()
        fields |= {"status": "queued", "created_at": now, "updated_at": now}
        cols = ", ".join(fields)
        with self.db:
            self.db.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values())
            )
        return self.task(fields["id"])

    def task(self, task_id: str) -> dict[str, Any] | None:
        return _row(self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def tasks(self, repo: str | None = None) -> list[dict[str, Any]]:
        if repo:
            rows = self.db.execute(
                "SELECT * FROM tasks WHERE repo = ? AND status != 'discarded' ORDER BY created_at DESC", (repo,)
            )
        else:
            rows = self.db.execute("SELECT * FROM tasks WHERE status != 'discarded' ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))
        return self.task(task_id)

    # reviews

    def save_review(self, root: str, review: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO reviews VALUES (?, ?, ?) ON CONFLICT(root) DO UPDATE SET "
                "payload = excluded.payload, created_at = excluded.created_at",
                (root, json.dumps(review), time.time()),
            )

    def review(self, root: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT payload, created_at FROM reviews WHERE root = ?", (root,)).fetchone()
        return {**json.loads(row["payload"]), "created_at": row["created_at"]} if row else None

    # runs (cost accounting)

    def start_run(self, kind: str, ref: str | None) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (kind, ref, started_at) VALUES (?, ?, ?)", (kind, ref, time.time())
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, num_turns: int, cost_usd: float | None, is_error: bool) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET ended_at = ?, num_turns = ?, cost_usd = ?, is_error = ? WHERE id = ?",
                (time.time(), num_turns, cost_usd, int(is_error), run_id),
            )

    def usage_since(self, since: float) -> dict[str, float]:
        row = self.db.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost FROM runs WHERE started_at >= ?",
            (since,),
        ).fetchone()
        return {"runs": row["runs"], "cost_usd": row["cost"]}

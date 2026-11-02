"""Conversations: one orchestrator turn at a time per conversation, streamed to the UI and persisted."""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, get_session_messages, tool

from .agents import (
    CHAT_MODE_PROMPT, CONTEXT_READ_ONLY, CONTEXT_REPO, CONTEXT_SCRATCH, ORCHESTRATOR_PROMPT, chat_subagents,
)
from .bus import Bus
from .checkpoints import Checkpoint, CheckpointError, Checkpoints
from .config import SCRATCH_DIR, Access, Config, Repo
from .guards import approval_detail, guard_hook
from .runner import run_agent
from .store import Store
from .tasks import TaskManager

BASE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "Bash", "WebSearch", "WebFetch", "TodoWrite", "Agent", "Task"]
TASK_TOOLS = ["mcp__workspace__start_task", "mcp__workspace__list_tasks"]
# Chat mode: read and discuss only. No file changes, no shell, no tasks.
CHAT_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "Agent", "Task"]
CHAT_BLOCKED = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell", "TodoWrite", *TASK_TOOLS]
RECAP_CHARS = 12_000
MENTION = re.compile(r"(?<![\w@])@([\w.\-/]+[\w])")
MENTION_FILE_LIMIT = 100_000
MENTION_TOTAL_LIMIT = 300_000
DEFAULT_TITLE = "New chat"
TITLE_PROMPT = "Write a 3-6 word title for this conversation. Reply with the title only, no quotes.\n\n{text}"
# Claude Code refuses a truncating resume whose discarded range holds more than the dropped turn.
FORK_REJECTED = "Resume rejected by --resume-drops-turn:"


@dataclass
class Rewind:
    """An edited message: its turn and everything after were removed; the new text runs next."""
    index: int                          # which user message (0-based) was edited
    fork: dict[str, str] | None = None  # resume options for the old session, or None to recap instead
    warnings: list[str] = field(default_factory=list)


def _recap(events: list[dict[str, Any]]) -> str:
    """Plain-text transcript used when a conversation starts a fresh agent session (e.g. after a repo change)."""
    lines = []
    for e in events:
        if e["type"] == "user":
            lines.append(f"User: {e['text']}")
        elif e["type"] == "text" and e.get("agent") == "main":
            lines.append(f"Assistant: {e['text']}")
    return "\n\n".join(lines)[-RECAP_CHARS:]


def _is_prompt(message: Any) -> bool:
    """Whether a transcript message starts a turn: a user message that isn't only tool results."""
    if message.type != "user":
        return False
    content = (message.message or {}).get("content")
    return not (isinstance(content, list) and content
                and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))


def attach_mentions(prompt: str, root: Path | None) -> str:
    """Append the contents of @path mentions that resolve to files inside the repo."""
    if root is None:
        return prompt
    blocks, total, seen = [], 0, set()
    for rel in MENTION.findall(prompt):
        path = (root / rel).resolve()
        if rel in seen or (root not in path.parents) or not path.is_file():
            continue
        seen.add(rel)
        try:
            data = path.read_bytes()
        except OSError:
            continue  # locked or unreadable: the agent can still try the Read tool
        if b"\0" in data[:8000]:
            continue  # binary
        if len(data) > MENTION_FILE_LIMIT or total + len(data) > MENTION_TOTAL_LIMIT:
            blocks.append(f'<file path="{rel}" omitted="too large; read it with the Read tool if needed" />')
            continue
        total += len(data)
        blocks.append(f'<file path="{rel}">\n{data.decode("utf-8", errors="replace")}\n</file>')
    if not blocks:
        return prompt
    return prompt + "\n\nFiles the user mentioned:\n" + "\n".join(blocks)


class ChatManager:
    def __init__(self, config: Config, store: Store, bus: Bus, tasks: TaskManager, checkpoints: Checkpoints):
        self.config, self.store, self.bus, self.tasks = config, store, bus, tasks
        self.checkpoints = checkpoints
        self.running: dict[str, asyncio.Task] = {}
        SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

    def busy(self) -> list[str]:
        return list(self.running)

    def start(self, conv_id: str, prompt: str) -> bool:
        if not prompt.strip() or conv_id in self.running or not self.store.conversation(conv_id):
            return False
        self.running[conv_id] = asyncio.create_task(self._turn(conv_id, prompt))
        return True

    def stop(self, conv_id: str) -> bool:
        if job := self.running.get(conv_id):
            job.cancel()
            return True
        return False

    def checkpoint_roots(self) -> list[Path]:
        """Where undoing a turn may write: every delegated repo plus the scratch folder."""
        return [r.path for r in self.config.repos] + [SCRATCH_DIR.resolve()]

    def edit(self, conv_id: str, index: int, text: str) -> None:
        """Rewind to just before the index-th user message, then send text in its place.

        Removes that message and everything after it, undoes the file changes of the removed turns,
        and resumes the Claude session from before the message (forking it), or recaps if it can't.
        Synchronous up to starting the new turn, so no other turn can start in between.
        """
        if not text.strip() or conv_id in self.running:
            raise ValueError("conversation is busy or the message is empty")
        conv = self.store.conversation(conv_id)
        if not conv:
            raise IndexError("no such conversation")
        rows = self.store.event_rows(conv_id)
        users = [(row_id, e) for row_id, e in rows if e["type"] == "user"]
        if not 0 <= index < len(users):
            raise IndexError("no such message")
        row_id, edited = users[index]
        # Everything that can fail comes before the first file is touched, so a refused edit
        # leaves files, checkpoints and history as they were.
        try:
            cwd = self._cwd(conv)
        except KeyError as exc:
            raise ValueError(f"the conversation's repo is no longer configured ({exc})") from exc
        fork = self._fork_point(edited, cwd)
        dropped = [e for i, e in rows if i >= row_id]
        warnings = self._undo([e["id"] for e in dropped if e["type"] == "checkpoint"])
        self.store.delete_events_from(conv_id, row_id)
        # The old session still holds the discarded turns: never resume it plainly again.
        self.store.update_conversation(conv_id, session_id=None)
        self.running[conv_id] = asyncio.create_task(self._turn(conv_id, text, Rewind(index, fork, warnings)))

    def _undo(self, checkpoint_ids: list[str]) -> list[str]:
        """Restore the files of discarded turns (checkpoint_ids is oldest first).

        Newest first, so a file several turns edited ends at its original from the oldest of them.
        """
        warnings = []
        restored = self.checkpoints.restored_ids(checkpoint_ids)
        for checkpoint_id in reversed(checkpoint_ids):
            if checkpoint_id in restored:
                continue  # already undone by hand
            try:
                self.checkpoints.restore(checkpoint_id, self.checkpoint_roots())
            except (CheckpointError, OSError) as exc:
                warnings.append(f"Could not undo all file changes of the discarded turns: {exc}")
        return warnings

    def _fork_point(self, edited: dict[str, Any], cwd: Path) -> dict[str, str] | None:
        """Resume options that branch the edited message's session from just before that message.

        Read from the transcript rather than from what we saw live: it also holds what a stopped turn
        wrote after we stopped listening, turns an earlier edit discarded from our history, and the
        UUIDs a forked session gave its copied history.
        """
        session_id, prompt_uuid = edited.get("session_id"), edited.get("prompt_uuid")
        if not session_id or not prompt_uuid:
            return None  # the message never reached a session, or predates rewind support
        try:
            messages = get_session_messages(session_id, directory=str(cwd))
        except Exception:
            messages = []
        uuids = [m.uuid for m in messages]
        if prompt_uuid not in uuids:
            return None  # transcript gone, or in another repo's project (the repo changed since)
        i = uuids.index(prompt_uuid)
        if i == 0:
            return None  # the message started its session: nothing earlier to keep
        fork = {"resume": session_id, "resume_session_at": uuids[i - 1]}
        # The CLI's guard only accepts a discarded range of one turn. When the session holds later
        # turns (ours, or ones an earlier edit already dropped), they go too, so truncate unguarded.
        if not any(_is_prompt(m) for m in messages[i + 1:]):
            fork["resume_drops_turn"] = prompt_uuid
        return fork

    async def _emit(self, conv_id: str, event: dict[str, Any], persist: bool = True) -> int | None:
        event_id = self.store.add_event(event, conversation_id=conv_id) if persist else None
        await self.bus.publish({**event, "conversation_id": conv_id})
        return event_id

    def _workspace_server(self, conv_id: str, repo: Repo):
        @tool(
            "start_task",
            "Start an implementation task: the implementer agent works in an isolated git worktree, "
            "then the reviewer checks it and the implementer fixes blocking findings. Returns immediately; "
            "the task runs in the background and the user reviews the diff in the IDE.",
            {"title": str, "spec": str},
        )
        async def start_task(args: dict[str, Any]) -> dict[str, Any]:
            task = await self.tasks.create(repo.name, args["title"], args["spec"], conv_id)
            await self._emit(conv_id, {"type": "task_link", "task_id": task["id"], "title": task["title"]})
            return {"content": [{"type": "text", "text": f"Started task {task['id']} on branch {task['branch']}."}]}

        @tool("list_tasks", "List agent tasks for this repo with their status.", {})
        async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
            rows = [f"{t['id']} [{t['status']}] {t['title']} ({t['branch']})" for t in self.store.tasks(repo.name)]
            return {"content": [{"type": "text", "text": "\n".join(rows) or "No tasks."}]}

        return create_sdk_mcp_server("workspace", tools=[start_task, list_tasks])

    def _cwd(self, conv: dict[str, Any]) -> Path:
        return self.config.repo(conv["repo"]).path if conv["repo"] else SCRATCH_DIR

    def _options(
        self, conv: dict[str, Any], checkpoint: Checkpoint | None = None, fork: dict[str, str] | None = None
    ) -> ClaudeAgentOptions:
        conv_id = conv["id"]
        repo = self.config.repo(conv["repo"]) if conv["repo"] else None
        if repo:
            cwd = repo.path
            context = CONTEXT_REPO.format(name=repo.name, access=repo.access.value)
            if repo.access is not Access.WRITE:
                context += "\n" + CONTEXT_READ_ONLY
            roots = list(self.config.repos)
        else:
            cwd = SCRATCH_DIR
            context = CONTEXT_SCRATCH.format(repos=", ".join(r.name for r in self.config.repos) or "none")
            roots = [*self.config.repos, Repo(name="scratch", path=SCRATCH_DIR, access=Access.WRITE)]

        async def approve(tool_name: str, args: dict[str, Any], reason: str) -> bool:
            return await self.bus.request_approval(
                {"conversation_id": conv_id}, tool_name, approval_detail(tool_name, args), reason
            )

        common = dict(
            cwd=cwd,
            add_dirs=[r.path for r in self.config.repos if r.path != cwd],
            setting_sources=["project"],
            model=self.config.models.orchestrator,
            max_turns=self.config.max_turns,
            include_partial_messages=True,
            resume=conv["session_id"],
        )
        if fork:
            common |= dict(fork, fork_session=True)
        if conv["mode"] == "chat":
            # Belt and braces: the tool list excludes writers, and the guard sees every root as read-only.
            read_only_roots = [Repo(name=r.name, path=r.path, access=Access.READ) for r in roots]
            return ClaudeAgentOptions(
                **common,
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": f"{CHAT_MODE_PROMPT}\n{context}"},
                agents={"researcher": chat_subagents(self.config.models)["researcher"]},
                tools=CHAT_TOOLS,
                allowed_tools=CHAT_TOOLS,
                disallowed_tools=CHAT_BLOCKED,
                hooks={"PreToolUse": [guard_hook(read_only_roots, cwd, approve, self.config.auto_approve)]},
            )

        can_start_tasks = repo is not None and repo.access is Access.WRITE
        hooks = {"PreToolUse": [guard_hook(roots, cwd, approve, self.config.auto_approve)]}
        if checkpoint:
            for event, matchers in checkpoint.hooks().items():
                hooks.setdefault(event, []).extend(matchers)
        return ClaudeAgentOptions(
            **common,
            system_prompt={"type": "preset", "preset": "claude_code", "append": f"{ORCHESTRATOR_PROMPT}\n{context}"},
            agents=chat_subagents(self.config.models),
            allowed_tools=BASE_TOOLS + (TASK_TOOLS if can_start_tasks else []),
            disallowed_tools=["PowerShell"],
            mcp_servers={"workspace": self._workspace_server(conv_id, repo)} if can_start_tasks else {},
            hooks=hooks,
        )

    async def _turn(self, conv_id: str, prompt: str, rewind: Rewind | None = None) -> None:
        # Everything runs inside the try, so a failure or Stop at any point still clears "busy".
        history: list[dict[str, Any]] = []
        checkpoint: Checkpoint | None = None
        # The user event also records where the prompt sits in a Claude session, so it can be edited
        # later: the session it went to and its transcript UUID. A fork's new session ID arrives later.
        user: dict[str, Any] = {}
        user_id: int | None = None
        try:
            conv = self.store.conversation(conv_id)
            history = self.store.events(conversation_id=conv_id)
            fork = rewind.fork if rewind else None
            if rewind:
                await self.bus.publish({"type": "conversation_rewound", "conversation_id": conv_id, "index": rewind.index})
            user = {"type": "user", "text": prompt, "session_id": conv["session_id"], "prompt_uuid": str(uuid.uuid4())}
            user_id = await self._emit(conv_id, user)
            for warning in rewind.warnings if rewind else []:
                await self._emit(conv_id, {"type": "warning", "message": warning})
            await self.bus.publish({"type": "chat_status", "conversation_id": conv_id, "busy": True})
            if conv["title"] == DEFAULT_TITLE:
                first_line = prompt.strip().splitlines()[0][:60]
                conv = self.store.update_conversation(conv_id, title=first_line or DEFAULT_TITLE)
                await self.bus.publish({"type": "conversation_updated", "conversation": conv})

            mentioned = attach_mentions(prompt, self._cwd(conv) if conv["repo"] else None)
            # Agent mode records the original of every file it edits, so the turn can be undone.
            checkpoint = Checkpoint(self._cwd(conv)) if conv["mode"] == "agent" else None
            while True:
                agent_prompt = mentioned
                if not fork and not conv["session_id"] and history:
                    agent_prompt = f"Earlier in this conversation:\n\n{_recap(history)}\n\n---\n\nNew message:\n{agent_prompt}"
                if not await self._run(conv_id, self._options(conv, checkpoint, fork), agent_prompt, user, fork):
                    break
                # The CLI refused the fork before running anything. That is deterministic, so instead of
                # retrying, start a fresh session with a recap of the kept messages.
                fork = None
                user["session_id"] = None
                await self._emit(conv_id, {
                    "type": "warning",
                    "message": "Could not rewind the agent session, so this continues in a new one "
                               "with a recap of the earlier messages.",
                })
        except asyncio.CancelledError:
            await self._emit(conv_id, {"type": "stopped"})
        except Exception as exc:  # surface CLI/auth failures in the UI instead of a dead turn
            await self._emit(conv_id, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            if user_id is not None:
                self.store.update_event(user_id, user)
            if checkpoint and (saved := self.checkpoints.save(checkpoint, conv_id)):
                await self._emit(conv_id, {"type": "checkpoint", **saved})
            self.running.pop(conv_id, None)
            await self.bus.publish({"type": "chat_status", "conversation_id": conv_id, "busy": False})
        if not history:
            asyncio.create_task(self._generate_title(conv_id, prompt))

    async def _run(
        self, conv_id: str, options: ClaudeAgentOptions, prompt: str, user: dict[str, Any], fork: dict[str, str] | None
    ) -> bool:
        """Run a turn's agent query. Returns True if the CLI refused the fork (then nothing ran)."""
        events = run_agent(options, prompt, self.store, self.bus, "chat", conv_id, user["prompt_uuid"])
        try:
            async with aclosing(events):
                async for event in events:
                    if event["type"] == "delta":
                        await self._emit(conv_id, event, persist=False)
                    elif event["type"] == "session":
                        # Saved right away, so a turn stopped before its result still continues its session.
                        user["session_id"] = event["session_id"]
                        self.store.update_conversation(conv_id, session_id=event["session_id"])
                    elif event["type"] in ("text", "tool"):
                        await self._emit(conv_id, event)
                    elif event["type"] == "result":
                        if fork and event["is_error"] and FORK_REJECTED in f"{event['errors']} {event.get('result')}":
                            return True
                        user["session_id"] = event["session_id"]
                        self.store.update_conversation(conv_id, session_id=event["session_id"])
                        await self._emit(conv_id, {
                            k: event[k] for k in ("type", "is_error", "num_turns", "cost_usd", "errors")
                        })
        except Exception as exc:
            if fork and FORK_REJECTED in str(exc):
                return True
            raise
        return False

    async def _generate_title(self, conv_id: str, prompt: str) -> None:
        options = ClaudeAgentOptions(
            model="haiku", system_prompt="You write concise conversation titles.",
            tools=[], max_turns=1, setting_sources=[],
        )
        try:
            async for event in run_agent(options, TITLE_PROMPT.format(text=prompt[:2000]), self.store, self.bus, "title", conv_id):
                if event["type"] == "result" and event["result"] and not event["is_error"]:
                    title = event["result"].strip().splitlines()[0].strip("\"'*#_` ")[:80]
                    conv = self.store.update_conversation(conv_id, title=title)
                    await self.bus.publish({"type": "conversation_updated", "conversation": conv})
        except Exception:
            pass  # the first-line title already set is a fine fallback

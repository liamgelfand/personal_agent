"""Conversations: one orchestrator turn at a time per conversation, streamed to the UI and persisted."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

from .agents import (
    CHAT_MODE_PROMPT, CONTEXT_READ_ONLY, CONTEXT_REPO, CONTEXT_SCRATCH, ORCHESTRATOR_PROMPT, chat_subagents,
)
from .bus import Bus
from .checkpoints import Checkpoint, Checkpoints
from .config import SCRATCH_DIR, Access, Config, Repo
from .guards import guard_hook
from .runner import run_agent, summarize_tool
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


def _recap(events: list[dict[str, Any]]) -> str:
    """Plain-text transcript used when a conversation starts a fresh agent session (e.g. after a repo change)."""
    lines = []
    for e in events:
        if e["type"] == "user":
            lines.append(f"User: {e['text']}")
        elif e["type"] == "text" and e.get("agent") == "main":
            lines.append(f"Assistant: {e['text']}")
    return "\n\n".join(lines)[-RECAP_CHARS:]


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
        data = path.read_bytes()
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
        if conv_id in self.running or not self.store.conversation(conv_id):
            return False
        self.running[conv_id] = asyncio.create_task(self._turn(conv_id, prompt))
        return True

    def stop(self, conv_id: str) -> bool:
        if job := self.running.get(conv_id):
            job.cancel()
            return True
        return False

    async def _emit(self, conv_id: str, event: dict[str, Any], persist: bool = True) -> None:
        if persist:
            self.store.add_event(event, conversation_id=conv_id)
        await self.bus.publish({**event, "conversation_id": conv_id})

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

    def _options(self, conv: dict[str, Any], checkpoint: Checkpoint | None = None) -> ClaudeAgentOptions:
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
                {"conversation_id": conv_id}, tool_name, summarize_tool(tool_name, args), reason
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
                hooks={"PreToolUse": [guard_hook(read_only_roots, cwd, approve)]},
            )

        can_start_tasks = repo is not None and repo.access is Access.WRITE
        hooks = {"PreToolUse": [guard_hook(roots, cwd, approve)]}
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

    async def _turn(self, conv_id: str, prompt: str) -> None:
        conv = self.store.conversation(conv_id)
        history = self.store.events(conversation_id=conv_id)
        await self._emit(conv_id, {"type": "user", "text": prompt})
        await self.bus.publish({"type": "chat_status", "conversation_id": conv_id, "busy": True})
        if conv["title"] == DEFAULT_TITLE:
            first_line = prompt.strip().splitlines()[0][:60]
            conv = self.store.update_conversation(conv_id, title=first_line or DEFAULT_TITLE)
            await self.bus.publish({"type": "conversation_updated", "conversation": conv})

        agent_prompt = attach_mentions(prompt, self._cwd(conv) if conv["repo"] else None)
        if not conv["session_id"] and history:
            agent_prompt = f"Earlier in this conversation:\n\n{_recap(history)}\n\n---\n\nNew message:\n{agent_prompt}"
        # Agent mode records the original of every file it edits, so the turn can be undone.
        checkpoint = Checkpoint(self._cwd(conv)) if conv["mode"] == "agent" else None
        try:
            options = self._options(conv, checkpoint)
            async for event in run_agent(options, agent_prompt, self.store, self.bus, "chat", conv_id):
                if event["type"] == "delta":
                    await self._emit(conv_id, event, persist=False)
                elif event["type"] in ("text", "tool"):
                    await self._emit(conv_id, event)
                elif event["type"] == "result":
                    self.store.update_conversation(conv_id, session_id=event["session_id"])
                    await self._emit(conv_id, {
                        k: event[k] for k in ("type", "is_error", "num_turns", "cost_usd", "errors")
                    })
        except asyncio.CancelledError:
            await self._emit(conv_id, {"type": "stopped"})
        except Exception as exc:  # surface CLI/auth failures in the UI instead of a dead turn
            await self._emit(conv_id, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            if checkpoint and (saved := self.checkpoints.save(checkpoint, conv_id)):
                await self._emit(conv_id, {"type": "checkpoint", **saved})
            self.running.pop(conv_id, None)
            await self.bus.publish({"type": "chat_status", "conversation_id": conv_id, "busy": False})
        if not history:
            asyncio.create_task(self._generate_title(conv_id, prompt))

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

"""Agent tasks: implementer -> reviewer -> fix loop, each task isolated in its own git worktree.

The pipeline is code-driven rather than left to the orchestrator, so every implementation
is reviewed. Changes are left uncommitted for the user to review and commit in the IDE.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from . import gitops
from .agents import load_agent
from .bus import Bus
from .config import WORKTREES_DIR, Access, Config
from .guards import approval_detail, guard_hook
from .review import has_open_findings, review_workspace
from .runner import run_agent
from .store import Store
from .workspace import get_workspace

MAX_FIX_ROUNDS = 2
IMPLEMENTER_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "Bash", "TodoWrite"]
ACTIVE = {"queued", "implementing", "reviewing", "fixing"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def _format_findings(review: dict[str, Any]) -> str:
    lines = [f"Reviewer summary: {review.get('summary', '')}"]
    for f in review.get("findings", []):
        if f["severity"] != "nit":
            lines.append(f"- [{f['severity']}] {f['file']}:{f['line']} {f['title']}: {f['detail']}")
    return "\n".join(lines)


class TaskManager:
    def __init__(self, config: Config, store: Store, bus: Bus):
        self.config, self.store, self.bus = config, store, bus
        self.running: dict[str, asyncio.Task] = {}
        self.discarding: set[str] = set()

    def recover(self) -> None:
        """Tasks that were mid-run when the server stopped can't be resumed; mark them stopped."""
        for task in self.store.tasks():
            if task["status"] in ACTIVE:
                self.store.update_task(task["id"], status="stopped", error="server restarted")

    async def _publish_task(self, task_id: str, **fields: Any) -> dict[str, Any]:
        task = self.store.update_task(task_id, **fields)
        await self.bus.publish({"type": "task_updated", "task": task})
        if self.bus.notifier and fields.get("status") in ("ready", "failed"):
            verb = "is ready for review" if fields["status"] == "ready" else "failed"
            await self.bus.notifier.send(f"Task {verb}", f"{task['title']} ({task['repo']})")
        return task

    async def _emit(self, task_id: str, event: dict[str, Any]) -> None:
        self.store.add_event(event, task_id=task_id)
        await self.bus.publish({**event, "task_id": task_id})

    async def create(self, repo_name: str, title: str, spec: str, conversation_id: str | None) -> dict[str, Any]:
        repo = self.config.repo(repo_name)
        if repo.access is not Access.WRITE:
            raise PermissionError(f"repo '{repo_name}' is read-only")
        if not await gitops.has_commits(repo.path):
            raise RuntimeError(f"repo '{repo_name}' has no commits yet; make an initial commit before starting tasks")
        task_id = uuid.uuid4().hex[:8]
        branch = f"agent/{_slug(title)}-{task_id}"
        worktree = WORKTREES_DIR / repo.name / task_id
        await gitops.add_worktree(repo.path, worktree, branch)
        task = self.store.create_task(
            id=task_id, conversation_id=conversation_id, repo=repo.name, title=title, spec=spec,
            branch=branch, base_sha=await gitops.head_sha(repo.path), worktree=str(worktree),
        )
        await self.bus.publish({"type": "task_updated", "task": task})
        self.running[task_id] = asyncio.create_task(self._pipeline(task_id))
        return task

    def stop(self, task_id: str) -> bool:
        if job := self.running.get(task_id):
            job.cancel()
            return True
        return False

    async def discard(self, task_id: str) -> None:
        # Wait for a running pipeline to actually end (and its Claude Code process to exit) before
        # deleting the worktree it is working in; on Windows the folder is locked until then.
        if job := self.running.get(task_id):
            self.discarding.add(task_id)
            job.cancel()
            await asyncio.wait({job}, timeout=30)
        task = self.store.task(task_id)
        repo = self.config.repo(task["repo"])
        await gitops.remove_worktree(repo.path, Path(task["worktree"]), task["branch"])
        await self._publish_task(task_id, status="discarded")

    async def merge(self, task_id: str, message: str | None) -> str:
        """Merge the task branch into the repo's checked-out branch, then remove the worktree.

        Uncommitted task changes are committed first with `message`. A failed merge is aborted,
        leaving the main checkout exactly as it was.
        """
        task = self.store.task(task_id)
        if task["status"] in ACTIVE:
            raise RuntimeError("wait for the task to finish (or stop it) before merging")
        repo = self.config.repo(task["repo"])
        worktree = Path(task["worktree"])
        if (await gitops.status(worktree))["files"]:
            if not (message and message.strip()):
                raise RuntimeError("the task has uncommitted changes; give a commit message to commit them")
            await gitops.stage(worktree, ["."])
            await gitops.commit(worktree, message.strip())
        if await gitops.head_sha(worktree) == task["base_sha"]:
            raise RuntimeError("the task branch has no changes to merge")
        try:
            await gitops.git(repo.path, "merge", "--no-ff", "-m", f"Merge task: {task['title']}", task["branch"])
        except gitops.GitError as exc:
            await gitops.git(repo.path, "merge", "--abort", check=False)
            raise RuntimeError(f"merge failed and was aborted; your checkout is unchanged. {exc}") from exc
        sha = (await gitops.git(repo.path, "rev-parse", "--short", "HEAD")).strip()
        await gitops.remove_worktree(repo.path, worktree, task["branch"])
        await self._publish_task(task_id, status="merged")
        return sha

    async def open_pr(self, task_id: str) -> str:
        task = self.store.task(task_id)
        worktree = Path(task["worktree"])
        if (await gitops.status(worktree))["files"]:
            raise RuntimeError("commit or discard the remaining changes before opening a PR")
        if (await gitops.head_sha(worktree)) == task["base_sha"]:
            raise RuntimeError("nothing committed on this branch yet")
        await gitops.git(worktree, "push", "-u", "origin", task["branch"])
        review = self.store.review(f"{task['repo']}@{task_id}")
        body = f"{task['spec']}\n\n---\nReview: {review['summary'] if review else 'not run'}\n\n" \
               "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create", "--head", task["branch"], "--title", task["title"], "--body", body,
            cwd=worktree, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace").strip())
        url = out.decode().strip().splitlines()[-1]
        await self._publish_task(task_id, pr_url=url)
        return url

    def _implementer_options(self, ws, session_id: str | None) -> ClaudeAgentOptions:
        scope = {"task_id": ws.task_id}

        async def approve(tool: str, args: dict[str, Any], reason: str) -> bool:
            return await self.bus.request_approval(scope, tool, approval_detail(tool, args), reason)

        implementer = load_agent("implementer", self.config.models.implementer)
        return ClaudeAgentOptions(
            cwd=ws.path,
            system_prompt={"type": "preset", "preset": "claude_code", "append": implementer.prompt},
            model=self.config.models.implementer,
            allowed_tools=IMPLEMENTER_TOOLS,
            disallowed_tools=["PowerShell", "Agent", "Task", "WebSearch", "WebFetch"],
            hooks={"PreToolUse": [guard_hook([ws.as_root()], ws.path, approve, self.config.auto_approve)]},
            setting_sources=["project"],
            max_turns=self.config.max_turns,
            resume=session_id,
        )

    async def _implement(self, ws, prompt: str, session_id: str | None) -> str | None:
        async for event in run_agent(
            self._implementer_options(ws, session_id), prompt, self.store, self.bus, "task", ws.task_id
        ):
            if event["type"] in ("text", "tool"):
                await self._emit(ws.task_id, {**event, "agent": "implementer"})
            elif event["type"] == "result":
                if event["is_error"]:
                    raise RuntimeError("; ".join(event["errors"] or ["implementer failed"]))
                return event["session_id"]
        return session_id

    async def _pipeline(self, task_id: str) -> None:
        task = self.store.task(task_id)
        ws = get_workspace(self.config, self.store, task["repo"], task_id)
        try:
            await self._publish_task(task_id, status="implementing")
            prompt = (
                f"Task: {task['title']}\n\n{task['spec']}\n\n"
                f"You are in an isolated git worktree on branch {task['branch']}. "
                "Do not commit: the user reviews and commits the changes."
            )
            session = await self._implement(ws, prompt, None)
            for round_no in range(MAX_FIX_ROUNDS + 1):
                await self._publish_task(task_id, status="reviewing", review_rounds=round_no + 1)
                await self._emit(task_id, {"type": "text", "agent": "pipeline", "text": f"Review round {round_no + 1}"})
                review = await review_workspace(ws, self.config, self.store, self.bus)
                await self._emit(task_id, {"type": "review", "agent": "reviewer", "review": review})
                if not has_open_findings(review) or round_no == MAX_FIX_ROUNDS:
                    break
                await self._publish_task(task_id, status="fixing")
                session = await self._implement(
                    ws, "The reviewer requested changes. Fix the blocking and should_fix issues, "
                    "then re-verify.\n\n" + _format_findings(review), session,
                )
            await self._publish_task(task_id, status="ready")
        except asyncio.CancelledError:
            if task_id not in self.discarding:  # a discard sets the final status itself
                await self._publish_task(task_id, status="stopped")
        except Exception as exc:
            await self._emit(task_id, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            await self._publish_task(task_id, status="failed", error=str(exc))
        finally:
            self.running.pop(task_id, None)
            self.discarding.discard(task_id)

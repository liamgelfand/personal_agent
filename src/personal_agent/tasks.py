"""Agent tasks: implementer -> reviewer -> fix loop, each task isolated in its own git worktree.

The pipeline is code-driven rather than left to the orchestrator, so every implementation
is reviewed. Changes are left uncommitted for the user to review and commit in the IDE.
A task interrupted by a server restart resumes on the next start (see TaskManager.recover).
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
PHASES = {"implementing", "reviewing", "fixing"}
MAX_RESUME_ATTEMPTS = 3
FIX_PROMPT = "The reviewer requested changes. Fix the blocking and should_fix issues, then re-verify.\n\n"
RESUME_PROMPT = (
    "The server restarted while you were working. Continue where you left off; check `git status` "
    "and the files first, since your last tool call may not have completed."
)
RESTART_NOTE = (
    "\n\nThe server restarted during an earlier attempt at this, and that session could not be "
    "resumed. The worktree may already hold uncommitted work from it: inspect it first (`git status`, "
    "`git diff`) and keep what is good rather than starting over."
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def _format_findings(review: dict[str, Any]) -> str:
    lines = [f"Reviewer summary: {review.get('summary', '')}"]
    for f in review.get("findings", []):
        if f["severity"] != "nit":
            lines.append(f"- [{f['severity']}] {f['file']}:{f['line']} {f['title']}: {f['detail']}")
    return "\n".join(lines)


class ResumeFailed(RuntimeError):
    """A resumed implementer session failed before doing any work (e.g. its session file is gone)."""


class TaskManager:
    def __init__(self, config: Config, store: Store, bus: Bus):
        self.config, self.store, self.bus = config, store, bus
        self.running: dict[str, asyncio.Task] = {}
        self.discarding: set[str] = set()
        self.stopping: set[str] = set()
        # Set when the server begins shutting down. Agent runs then fail or end early (on a console
        # Ctrl+C the Claude children get the signal too); those endings must not become a final status.
        self.shutting_down = False

    async def shutdown(self) -> None:
        """End running pipelines without changing their status, so the next start resumes them.

        Cancelling a run makes the SDK close its Claude process (stdin EOF, then terminate, then
        kill: about 15s at worst), so wait for that rather than leave a process editing the worktree.
        """
        self.shutting_down = True
        jobs = set(self.running.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.wait(jobs, timeout=20)

    def recover(self) -> None:
        """Re-queue tasks that were mid-run when the server stopped; call with the event loop running.

        Implementing and fixing continue the implementer's Claude session; reviewing reruns the
        current review round. Each task gets MAX_RESUME_ATTEMPTS restarts, so a task that keeps
        crashing the server ends as failed instead of looping.
        """
        for task in self.store.tasks():
            if task["status"] not in ACTIVE:
                continue
            if not Path(task["worktree"]).is_dir():
                self.store.update_task(task["id"], status="stopped", error="server restarted and the worktree is gone")
            elif task["resume_attempts"] >= MAX_RESUME_ATTEMPTS:
                error = f"the server restarted {task['resume_attempts'] + 1} times during this task; gave up resuming"
                self.store.add_event({"type": "error", "message": error}, task_id=task["id"])
                self.store.update_task(task["id"], status="failed", error=error)
            else:
                self.store.update_task(task["id"], resume_attempts=task["resume_attempts"] + 1)
                self.running[task["id"]] = asyncio.create_task(self._pipeline(task["id"], resume=True))

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
            self.stopping.add(task_id)
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

    async def _implement(self, ws, prompt: str, session_id: str | None) -> None:
        """Run the implementer, saving its session id as soon as it is known so a restart can resume it.

        Raises ResumeFailed when a resumed session (`session_id` set) fails before any text or tool use.
        """
        worked = False
        try:
            async for event in run_agent(
                self._implementer_options(ws, session_id), prompt, self.store, self.bus, "task", ws.task_id
            ):
                if event["type"] == "session":
                    self.store.update_task(ws.task_id, session_id=event["session_id"])
                elif event["type"] in ("text", "tool"):
                    worked = True
                    await self._emit(ws.task_id, {**event, "agent": "implementer"})
                elif event["type"] == "result":
                    self.store.update_task(ws.task_id, session_id=event["session_id"])
                    if event["is_error"]:
                        raise RuntimeError("; ".join(event["errors"] or ["implementer failed"]))
                    return
            raise RuntimeError("the implementer ended without a result")
        except Exception as exc:
            if session_id and not worked:
                raise ResumeFailed(str(exc)) from exc
            raise

    async def _resume_implementer(self, ws, session_id: str | None, fresh_prompt: str) -> None:
        """After a restart: continue the saved session, or rerun the phase from `fresh_prompt`.

        Only a session that could not be resumed at all is rerun; a resumed session that fails
        later fails the task as usual.
        """
        if session_id:
            try:
                await self._implement(ws, RESUME_PROMPT, session_id)
                return
            except ResumeFailed as exc:
                if self.shutting_down:
                    raise
                text = f"Could not resume session, restarted phase ({exc})"
        else:
            text = "No saved session to resume, restarted phase from the spec"
        await self._emit(ws.task_id, {"type": "resumed", "agent": "pipeline", "text": text})
        await self._implement(ws, fresh_prompt + RESTART_NOTE, None)

    async def _pipeline(self, task_id: str, resume: bool = False) -> None:
        task = self.store.task(task_id)
        ws = get_workspace(self.config, self.store, task["repo"], task_id)
        try:
            spec_prompt = (
                f"Task: {task['title']}\n\n{task['spec']}\n\n"
                f"You are in an isolated git worktree on branch {task['branch']}. "
                "Do not commit: the user reviews and commits the changes."
            )
            # The phase to pick up after a restart; None once it has been resumed (or on a fresh run).
            resume_phase = None
            start_round = 0
            if resume:
                resume_phase = task["phase"] or (task["status"] if task["status"] in PHASES else "implementing")
                start_round = max(task["review_rounds"] - 1, 0)
                await self._emit(task_id, {"type": "resumed", "agent": "pipeline", "phase": resume_phase,
                                           "text": f"Resumed after server restart ({resume_phase})"})
            if resume_phase in (None, "implementing"):
                await self._publish_task(task_id, status="implementing", phase="implementing")
                if resume_phase:
                    await self._resume_implementer(ws, task["session_id"], spec_prompt)
                else:
                    await self._implement(ws, spec_prompt, None)
                resume_phase = None
            for round_no in range(start_round, MAX_FIX_ROUNDS + 1):
                if resume_phase != "fixing":  # reviews are read-only, so a resumed one just reruns
                    await self._publish_task(task_id, status="reviewing", phase="reviewing", review_rounds=round_no + 1)
                    await self._emit(task_id, {"type": "text", "agent": "pipeline", "text": f"Review round {round_no + 1}"})
                    review = await review_workspace(ws, self.config, self.store, self.bus)
                    if self.shutting_down:  # an interrupted review is no verdict; rerun it on the next start
                        return
                    await self._emit(task_id, {"type": "review", "agent": "reviewer", "review": review})
                    if not has_open_findings(review) or round_no == MAX_FIX_ROUNDS:
                        break
                await self._publish_task(task_id, status="fixing", phase="fixing")
                if resume_phase == "fixing":
                    findings = _format_findings(self.store.review(ws.key) or {})
                    await self._resume_implementer(ws, task["session_id"], f"{spec_prompt}\n\n{FIX_PROMPT}{findings}")
                else:
                    await self._implement(ws, FIX_PROMPT + _format_findings(review), self.store.task(task_id)["session_id"])
                resume_phase = None
            await self._publish_task(task_id, status="ready")
        except asyncio.CancelledError:
            # Stop and discard are user actions; any other cancellation is the server shutting down,
            # which leaves the task active so the next start resumes it.
            if task_id in self.stopping:
                await self._publish_task(task_id, status="stopped")
        except Exception as exc:
            if self.shutting_down and task_id not in self.stopping:
                return  # the agent died with the server; leave the task to resume
            await self._emit(task_id, {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            await self._publish_task(task_id, status="failed", error=str(exc))
        finally:
            self.running.pop(task_id, None)
            self.discarding.discard(task_id)
            self.stopping.discard(task_id)

"""Agent tasks: implementer -> reviewer -> fix loop, each task isolated in its own git worktree.

The pipeline is code-driven rather than left to the orchestrator, so every implementation
is reviewed. Once a task is ready the server commits what the implementer left, pushes the
branch and opens a PR (see TaskManager._finish); nothing is ever merged automatically.
A task interrupted by a server restart resumes on the next start (see TaskManager.recover).
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from . import bootstrap, gitops
from .agents import load_agent
from .bus import Bus
from .config import WORKTREES_DIR, Access, Config
from .guards import approval_detail, guard_hook
from .review import has_open_findings, review_workspace
from .runner import run_agent
from .store import Store
from .workspace import get_workspace

IMPLEMENTER_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "Bash", "TodoWrite"]
ACTIVE = {"queued", "implementing", "reviewing", "fixing"}
PHASES = {"implementing", "reviewing", "fixing"}
MAX_RESUME_ATTEMPTS = 3
FIX_PROMPT = (
    "The reviewer found blocking issues. Fix them (and the should_fix issues, if quick), then re-verify: "
    "the relevant tests and the type check, not a full frontend build.\n\n"
)
RESUME_PROMPT = (
    "The server restarted while you were working. Continue where you left off; check `git status` "
    "and the files first, since your last tool call may not have completed."
)
RESTART_NOTE = (
    "\n\nThe server restarted during an earlier attempt at this, and that session could not be "
    "resumed. The worktree may already hold uncommitted work from it: inspect it first (`git status`, "
    "`git diff`) and keep what is good rather than starting over."
)


CO_AUTHOR = "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
VERIFY_SECTION = re.compile(r"^\s*(?:#+\s*)?verify\b", re.IGNORECASE | re.MULTILINE)

# New files with these names are never committed and pushed by the finish step.
SECRET_FILES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx", "id_rsa*", "id_dsa*",
    "id_ecdsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc", "credentials*", "*secret*", "*.tfstate",
)


async def _gh(cwd: Path, *args: str) -> str:
    """Run the GitHub CLI. It authenticates from its own login or a GH_TOKEN/GITHUB_TOKEN env var."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("gh is not installed") from None
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip() or f"gh {args[0]} failed")
    return out.decode(errors="replace").strip()


async def _gh_pr(cwd: Path, base: str, branch: str, title: str, body: str, draft: bool = False) -> str:
    """URL of the open PR for `branch`, creating it against `base` if there is none."""
    if existing := await _gh(cwd, "pr", "list", "--head", branch, "--json", "url", "--jq", ".[0].url"):
        return existing
    args = ["pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body]
    out = await _gh(cwd, *args, *(["--draft"] if draft else []))
    return out.splitlines()[-1]


def _secret_like(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILES)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def _format_findings(review: dict[str, Any]) -> str:
    lines = [f"Reviewer summary: {review.get('summary', '')}"]
    for f in review.get("findings", []):
        if f["severity"] != "nit":
            lines.append(f"- [{f['severity']}] {f['file']}:{f['line']} {f['title']}: {f['detail']}")
    return "\n".join(lines)


def _duration(seconds: float, short: bool = False) -> str:
    minutes, secs = divmod(round(seconds), 60)
    if not minutes:
        return f"{secs}s"
    return f"{round(seconds / 60)}m" if short else f"{minutes}m {secs}s"


class ResumeFailed(RuntimeError):
    """A resumed implementer session failed before doing any work (e.g. its session file is gone)."""


class TaskManager:
    def __init__(self, config: Config, store: Store, bus: Bus):
        self.config, self.store, self.bus = config, store, bus
        self.running: dict[str, asyncio.Task] = {}
        self.discarding: set[str] = set()
        self.stopping: set[str] = set()
        # One finish step per task at a time, e.g. the pipeline's and an "Open PR" click.
        self.finish_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
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
            message = f"{task['title']} ({task['repo']})"
            if task["pr_url"]:
                message += f"\n{'Compare page' if task['pr_state'] == 'compare' else 'PR'}: {task['pr_url']}"
            await self.bus.notifier.send(f"Task {verb}", message)
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
        # A running finish step (push, gh) completes first rather than lose its worktree halfway.
        async with self.finish_locks[task_id]:
            await self._discard(task_id)

    async def _discard(self, task_id: str) -> None:
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
        leaving the main checkout exactly as it was. Waits for a running finish step (the status
        is already ready while the pipeline pushes), so that never loses its worktree halfway.
        """
        async with self.finish_locks[task_id]:
            return await self._merge(task_id, message)

    async def _merge(self, task_id: str, message: str | None) -> str:
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
        """The "Open PR" button: rerun the finish step and report its failure, if any."""
        task = self.store.task(task_id)
        if task["status"] in ACTIVE | {"merged", "discarded"}:
            raise RuntimeError(f"cannot open a PR for a task that is {task['status']}")
        task = await self.finish(task_id)
        if not task["pr_url"]:
            raise RuntimeError(task["finish_error"] or "could not open a PR")
        return task["pr_url"]

    async def finish(self, task_id: str) -> dict[str, Any]:
        return await self._publish_task(task_id, **await self._finish(task_id))

    async def _finish(self, task_id: str) -> dict[str, Any]:
        """Commit leftover changes, push the task branch and open a PR; returns the task fields it set.

        Runs in the server process, not through the agent guard. Never merges and never pushes to
        the default branch. Does nothing once a PR exists; a compare-page fallback is retried, since
        `gh pr list` keeps that from opening a duplicate. Failures are returned as `finish_error`.
        Serialized per task, and the result is stored before the lock is released, so a second run
        sees the first one's PR.
        """
        async with self.finish_locks[task_id]:
            fields = await self._finish_locked(task_id)
            if fields:
                self.store.update_task(task_id, **fields)
            return fields

    async def _finish_locked(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task["pr_url"] and task["pr_state"] != "compare":
            return {}
        if task["status"] in ("merged", "discarded"):  # merged or discarded while this run waited
            return {"finish_error": f"the task was {task['status']}, so no PR was opened"}
        try:
            repo = self.config.repo(task["repo"])
            if repo.access is not Access.WRITE:
                raise RuntimeError(f"repo '{repo.name}' is read-only")
            worktree, branch = Path(task["worktree"]), task["branch"]
            base = await gitops.default_branch(repo.path)
            if branch in (base, "main", "master"):
                raise RuntimeError(f"refusing to push the default branch '{branch}'")
            if not await gitops.remote_url(worktree):
                raise RuntimeError("the repo has no 'origin' remote to push to")
            files = (await gitops.status(worktree))["files"]
            new = [f["path"] for f in files if f["untracked"] or f["index"] == "A"]
            if secrets := [p for p in new if _secret_like(p)]:
                raise RuntimeError(
                    f"new files that may hold secrets: {', '.join(secrets)}. Delete them, add them to "
                    ".gitignore or commit them yourself, then use Open PR"
                )
            if files:
                await gitops.stage(worktree, ["."])
                await gitops.commit(worktree, f"{task['title']}\n\nTask {task_id}\n\n{CO_AUTHOR}")
            if await gitops.head_sha(worktree) == task["base_sha"]:
                raise RuntimeError("the task made no changes, so there is nothing to push")
            # An explicit refspec, so the push can only ever update the task's own branch.
            await gitops.git(worktree, "push", "-u", "origin", f"refs/heads/{branch}:refs/heads/{branch}")
        except Exception as exc:
            return await self._finish_failed(task_id, f"Could not push the branch: {exc}")
        review = self.store.review(f"{task['repo']}@{task_id}")
        draft = bool(review and has_open_findings(review))  # the pipeline never gets here then; Open PR does
        try:
            body = self._pr_body(task, review)
            url, state, error = await _gh_pr(worktree, base, branch, task["title"], body, draft), "open", None
        except Exception as exc:
            error = f"gh could not open the PR ({exc})"
            remote = gitops.github_repo(await gitops.remote_url(worktree) or "")
            if not remote:
                return await self._finish_failed(task_id, f"{error}; origin is not a GitHub remote")
            url, state = gitops.compare_url(*remote, base, branch, task["title"]), "compare"
            await self._emit(task_id, {"type": "warning", "message": f"{error}. Open the compare page to create it."})
        text = (f"Opened {'draft ' if draft else ''}pull request" if state == "open"
                else "Pushed the branch; open the compare page to create the PR")
        if new:
            text += f". Committed new files: {', '.join(new)}"
        await self._emit(task_id, {"type": "finished", "agent": "pipeline", "url": url, "pr_state": state, "text": text})
        return {"pr_url": url, "pr_state": state, "finish_error": error}

    async def _finish_failed(self, task_id: str, message: str) -> dict[str, Any]:
        await self._emit(task_id, {"type": "warning", "message": message})
        return {"finish_error": message}

    @staticmethod
    def _pr_body(task: dict[str, Any], review: dict[str, Any] | None) -> str:
        if review:
            count = len(review.get("findings", []))
            summary = f"{review.get('summary', '')} ({count} finding{'' if count == 1 else 's'})"
        else:
            summary = "not run"
        parts = [task["spec"], "---", f"Review: {summary}"]
        if review and has_open_findings(review):
            open_findings = _format_findings(review).split(chr(10), 1)[1:]
            parts.append("**The final review still has open findings:**" + "".join(chr(10) + f for f in open_findings))
        if VERIFY_SECTION.search(task["spec"]):
            parts.append("How to test: follow the Verify section above.")
        return "\n\n".join([*parts, "🤖 Generated with [Claude Code](https://claude.com/claude-code)"])

    def _implementer_options(self, ws, session_id: str | None) -> ClaudeAgentOptions:
        scope = {"task_id": ws.task_id}

        async def approve(tool: str, args: dict[str, Any], reason: str) -> bool:
            return await self.bus.request_approval(scope, tool, approval_detail(tool, args), reason)

        implementer = load_agent("implementer", self.config.models.implementer)
        prompt = implementer.prompt + "\n\n" + bootstrap.environment_section(ws.repo, ws.path, "implementer")
        return ClaudeAgentOptions(
            cwd=ws.path,
            system_prompt={"type": "preset", "preset": "claude_code", "append": prompt},
            model=self.config.models.implementer,
            allowed_tools=IMPLEMENTER_TOOLS,
            disallowed_tools=["PowerShell", "Agent", "Task", "WebSearch", "WebFetch"],
            hooks={"PreToolUse": [
                guard_hook([ws.as_root()], ws.path, approve, self.config.auto_approve),
                bootstrap.dependency_link_hook(ws.path, lambda links, command: self._unlinked(ws.task_id, links, command)),
            ]},
            setting_sources=["project"],
            max_turns=self.config.max_turns,
            resume=session_id,
        )

    async def _implement(self, ws, prompt: str, session_id: str | None) -> dict[str, Any]:
        """Run the implementer, saving its session id as soon as it is known so a restart can resume it.

        Returns the run's turns and cost. Raises ResumeFailed when a resumed session (`session_id` set)
        fails before any text or tool use.
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
                    return {"turns": event.get("num_turns"), "cost_usd": event.get("cost_usd")}
            raise RuntimeError("the implementer ended without a result")
        except Exception as exc:
            if session_id and not worked:
                raise ResumeFailed(str(exc)) from exc
            raise

    async def _resume_implementer(self, ws, session_id: str | None, fresh_prompt: str) -> dict[str, Any]:
        """After a restart: continue the saved session, or rerun the phase from `fresh_prompt`.

        Only a session that could not be resumed at all is rerun; a resumed session that fails
        later fails the task as usual.
        """
        if session_id:
            try:
                return await self._implement(ws, RESUME_PROMPT, session_id)
            except ResumeFailed as exc:
                if self.shutting_down:
                    raise
                text = f"Could not resume session, restarted phase ({exc})"
        else:
            text = "No saved session to resume, restarted phase from the spec"
        await self._emit(ws.task_id, {"type": "resumed", "agent": "pipeline", "text": text})
        return await self._implement(ws, fresh_prompt + RESTART_NOTE, None)

    async def _bootstrap(self, ws) -> None:
        """Prepare a new worktree: link node_modules from the main checkout, run the repo's setup.

        Nothing here fails the task; problems are logged and the implementer copes as before.
        """
        try:
            if linked := await gitops.link_dependencies(ws.repo.path, ws.path):
                text = "Linked from the main checkout: " + ", ".join(linked)
                await self._emit(ws.task_id, {"type": "text", "agent": "pipeline", "text": text})
        except (OSError, gitops.GitError) as exc:
            await self._emit(ws.task_id, {"type": "warning", "message": f"Could not link node_modules: {exc}"})
        for failure in await bootstrap.run_setup(ws.repo.setup, ws.path):
            await self._emit(ws.task_id, {"type": "warning", "message": f"Setup command failed (continuing): {failure}"})

    async def _unlinked(self, task_id: str, links: list[str], command: str) -> None:
        text = f"Replaced the shared {', '.join(links)} with the worktree's own before `{command[:200]}`"
        await self._emit(task_id, {"type": "text", "agent": "pipeline", "text": text})

    async def _record_phase(
        self, task_id: str, phase: str, round_no: int | None, started: float, stats: dict[str, Any] | None = None,
    ) -> None:
        ended = time.time()
        stats = stats or {}
        seconds = round(ended - started, 1)
        parts = [f"{phase.capitalize()}{f' {round_no}' if round_no else ''} took {_duration(seconds)}"]
        if stats.get("turns"):
            parts.append(f"{stats['turns']} turns")
        if stats.get("cost_usd") is not None:
            parts.append(f"${stats['cost_usd']:.2f} API-equivalent")
        await self._emit(task_id, {
            "type": "phase", "agent": "pipeline", "phase": phase, "round": round_no, "started_at": started,
            "ended_at": ended, "seconds": seconds, "turns": stats.get("turns"), "cost_usd": stats.get("cost_usd"),
            "text": " · ".join(parts),
        })

    def _timing_summary(self, task_id: str) -> str:
        """e.g. "bootstrap 4s, implement 9m, review 5m", from every phase event (across restarts too)."""
        phases = [e for e in self.store.events(task_id=task_id) if e["type"] == "phase"]
        return ", ".join(f"{e['phase']} {_duration(e['seconds'], short=True)}" for e in phases)

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
            max_rounds = self.config.max_review_rounds
            review: dict[str, Any] = {}
            if resume:
                resume_phase = task["phase"] or (task["status"] if task["status"] in PHASES else "implementing")
                start_round = min(max(task["review_rounds"] - 1, 0), max_rounds - 1)
                await self._emit(task_id, {"type": "resumed", "agent": "pipeline", "phase": resume_phase,
                                           "text": f"Resumed after server restart ({resume_phase})"})
            # Normally done once on a fresh run; also redone if a restart cut it short (it is idempotent).
            if not any(e["type"] == "phase" and e["phase"] == "bootstrap" for e in self.store.events(task_id=task_id)):
                started = time.time()
                await self._bootstrap(ws)
                await self._record_phase(task_id, "bootstrap", None, started)
            if resume_phase in (None, "implementing"):
                await self._publish_task(task_id, status="implementing", phase="implementing")
                started = time.time()
                if resume_phase:
                    stats = await self._resume_implementer(ws, task["session_id"], spec_prompt)
                else:
                    stats = await self._implement(ws, spec_prompt, None)
                await self._record_phase(task_id, "implement", None, started, stats)
                resume_phase = None
            # Another fix + review round only follows open (blocking or should_fix) findings, up to max_review_rounds reviews.
            for round_no in range(start_round, max_rounds):
                if resume_phase != "fixing":  # reviews are read-only, so a resumed one just reruns
                    await self._publish_task(task_id, status="reviewing", phase="reviewing", review_rounds=round_no + 1)
                    await self._emit(task_id, {"type": "text", "agent": "pipeline", "text": f"Review round {round_no + 1}"})
                    started, stats = time.time(), {}
                    review = await review_workspace(ws, self.config, self.store, self.bus, stats=stats)
                    if self.shutting_down:  # an interrupted review is no verdict; rerun it on the next start
                        return
                    await self._emit(task_id, {"type": "review", "agent": "reviewer", "review": review})
                    await self._record_phase(task_id, "review", round_no + 1, started, stats)
                    if not has_open_findings(review):
                        break
                    if round_no == max_rounds - 1:
                        open_ = [f for f in review["findings"] if f["severity"] in ("blocking", "should_fix")]
                        await self._emit(task_id, {"type": "warning", "message": (
                            f"Review limit reached ({max_rounds} rounds) with {len(open_)} open finding(s) "
                            "still open: " + "; ".join(f"{f['file']}:{f['line']} {f['title']}" for f in open_)
                        )})
                        break
                await self._publish_task(task_id, status="fixing", phase="fixing")
                started = time.time()
                if resume_phase == "fixing":
                    findings = _format_findings(self.store.review(ws.key) or {})
                    stats = await self._resume_implementer(ws, task["session_id"], f"{spec_prompt}\n\n{FIX_PROMPT}{findings}")
                else:
                    stats = await self._implement(ws, FIX_PROMPT + _format_findings(review), self.store.task(task_id)["session_id"])
                await self._record_phase(task_id, "fix", round_no + 1, started, stats)
                resume_phase = None
            summary = self._timing_summary(task_id)
            await self._emit(task_id, {"type": "timing", "agent": "pipeline", "text": f"Finished: {summary}", "summary": summary})
            # Ready first, so a restart during the push does not rerun the review; the published
            # update then carries the PR link, for the notification.
            self.store.update_task(task_id, status="ready")
            if has_open_findings(review):
                # Unresolved findings: nothing leaves the machine until the user decides (Open PR).
                fields = {"finish_error": "the final review still has open findings, so no PR was opened; "
                                          "fix them, or use Open PR for a draft PR that lists them"}
                await self._emit(task_id, {"type": "warning", "message": fields["finish_error"]})
            else:
                fields = await self._finish(task_id)
            await self._publish_task(task_id, status="ready", timing=summary, **fields)
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

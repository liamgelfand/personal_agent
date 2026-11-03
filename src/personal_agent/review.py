"""Reviewer agent run over a workspace's uncommitted changes, returning structured findings."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from . import gitops
from .agents import load_agent
from .bootstrap import dependency_link_hook, environment_section
from .bus import Bus
from .config import Config
from .guards import approval_detail, guard_hook
from .runner import run_agent
from .store import Store
from .workspace import Workspace

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "changes_requested"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "path relative to the repo root"},
                    "line": {"type": "integer", "description": "line number in the new version of the file"},
                    "severity": {"type": "string", "enum": ["blocking", "should_fix", "nit"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string", "description": "the problem and the input/state that triggers it"},
                },
                "required": ["file", "line", "severity", "title", "detail"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """\
Review the uncommitted changes in this repository (staged and unstaged, plus new untracked
files). Start with `git status` and `git diff HEAD`, then read the surrounding code and any
new files. Report each finding against the file and line in the new version of the file.
"""

TASK_REVIEW_PROMPT = """\
Review this task's changes against its base commit {base}. Run `git status` and `git diff {base}`
(the working tree against the base: commits and uncommitted changes alike), and read new untracked
files. Review the diff; read surrounding code only where a hunk needs it, not whole files. Report
each finding against the file and line in the new version of the file.
"""

READ_TOOLS = ["Read", "Grep", "Glob", "Bash"]


def has_blocking_findings(review: dict[str, Any]) -> bool:
    """Only blocking findings warrant another fix round; should_fix and nit stay in the review."""
    return any(f["severity"] == "blocking" for f in review.get("findings", []))


async def review_workspace(
    ws: Workspace, config: Config, store: Store, bus: Bus, stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Review the workspace's changes; `stats`, if given, receives the run's turns and cost."""
    if not (await gitops.status(ws.path))["files"]:
        review = {"verdict": "approve", "summary": "No changes to review.", "findings": []}
        store.save_review(ws.key, review)
        return review

    scope = {"workspace": ws.key}

    async def approve(tool: str, args: dict[str, Any], reason: str) -> bool:
        return await bus.request_approval(scope, tool, approval_detail(tool, args), reason)

    reviewer = load_agent("reviewer", config.models.reviewer)
    system_prompt, prompt = reviewer.prompt, REVIEW_PROMPT
    hooks = [guard_hook([ws.as_root()], ws.path, approve, config.auto_approve)]
    if ws.task_id and (task := store.task(ws.task_id)):
        system_prompt += "\n\n" + environment_section(ws.repo, ws.path, "reviewer")
        prompt = TASK_REVIEW_PROMPT.format(base=task["base_sha"])

        async def unlinked(links: list[str], command: str) -> None:
            text = f"Replaced the shared {', '.join(links)} with the worktree's own before `{command[:200]}`"
            store.add_event({"type": "text", "agent": "pipeline", "text": text}, task_id=ws.task_id)

        hooks.append(dependency_link_hook(ws.path, unlinked))
    options = ClaudeAgentOptions(
        cwd=ws.path,
        system_prompt=system_prompt,
        model=config.models.reviewer,
        tools=READ_TOOLS,
        allowed_tools=READ_TOOLS,
        hooks={"PreToolUse": hooks},
        setting_sources=["project"],
        output_format={"type": "json_schema", "schema": REVIEW_SCHEMA},
        max_turns=40,
    )
    await bus.publish({"type": "review_started", **scope})
    review: dict[str, Any] | None = None
    async for event in run_agent(options, prompt, store, bus, "review", ws.key):
        if event["type"] == "tool":
            await bus.publish({"type": "review_progress", "name": event["name"], "summary": event["summary"], **scope})
        elif event["type"] == "result":
            if stats is not None:
                stats.update(turns=event["num_turns"], cost_usd=event["cost_usd"])
            review = event["structured_output"]
            if not isinstance(review, dict):
                review = {
                    "verdict": "changes_requested" if event["is_error"] else "approve",
                    "summary": event["result"] or "Review did not return structured findings.",
                    "findings": [],
                }
    if review is None:
        review = {"verdict": "changes_requested", "summary": "Review was interrupted.", "findings": []}
    store.save_review(ws.key, review)
    await bus.publish({"type": "review", "review": store.review(ws.key), **scope})
    return review

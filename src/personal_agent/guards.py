"""PreToolUse guard that keeps agents inside their allowed roots and gates risky commands.

Each call is allowed, denied, or sent to the user for approval. This is a guardrail,
not a sandbox: Bash in a writable root can still reach anything the OS user can.
The hard boundaries are the repo-scoped GitHub token and task worktrees.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import HookContext, HookMatcher

from .config import PROJECT_ROOT, Access, Repo

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
PATH_TOOLS = WRITE_TOOLS | {"Read", "Glob", "Grep"}
PATH_KEYS = ("file_path", "notebook_path", "path")

BLOCKED_COMMANDS = [
    (re.compile(r"\bgit\s+push\b.*(--force\b|-f\b|--force-with-lease)"), "force push"),
    (re.compile(r"\bgit\s+push\b.*\b(main|master)\b"), "push to main/master"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+-\w*f"), "git clean -f"),
    (re.compile(r"\brm\s+-\w*r\w*\s+(/|~|\$HOME|[A-Za-z]:[\\/])\s*$"), "recursive delete of a root"),
    (re.compile(r"\bgh\s+repo\s+delete\b"), "deleting a GitHub repo"),
    (re.compile(r"\bgh\s+pr\s+merge\b"), "merging a PR (you merge, not the agent)"),
]

# Allowed only after the user approves in the UI.
ASK_COMMANDS = [
    (re.compile(r"\bgit\s+push\b"), "push to a remote"),
    (re.compile(r"\bgit\s+commit\b"), "create a commit"),
    (re.compile(r"\bgh\s+(pr|issue|release)\s+(create|edit|close|comment|review)\b"), "write to GitHub"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+(install|i|add|publish)\b"), "install or publish packages"),
    (re.compile(r"\b(pip|pip3)\s+install\b|\buv\s+(add|pip\s+install)\b"), "install packages"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(ba|z)?sh\b"), "run a downloaded script"),
    (re.compile(r"\bdocker\b"), "run docker"),
]

# The agent may work on its own repo, but changing what it is allowed to do needs your approval.
# Only the live checkout is protected; copies in task worktrees are reviewed before they land.
PROTECTED_FILES = {
    (PROJECT_ROOT / "config.yaml").resolve(): "edit the agent's repo access config",
    (PROJECT_ROOT / ".env").resolve(): "edit the agent's environment file",
    (PROJECT_ROOT / "src" / "personal_agent" / "guards.py").resolve(): "edit the agent's safety guard",
}

# In read-only roots, Bash may only run these (every segment of a pipeline/chain).
READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "pwd", "echo",
    "which", "file", "stat", "du", "diff", "sort", "uniq", "cut", "less", "jq",
}
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "branch", "rev-parse", "ls-files", "grep"}
HARMLESS_REDIRECTS = re.compile(r"\d?>&\d|[12]?>\s*/dev/null")
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")


@dataclass
class Decision:
    action: Literal["deny", "ask"]
    reason: str


def _read_only_violation(command: str) -> str | None:
    command = HARMLESS_REDIRECTS.sub("", command)
    if ">" in command or "`" in command or "$(" in command:
        return "repo is read-only: no redirection or command substitution"
    for segment in SEGMENT_SPLIT.split(command):
        words = segment.split()
        if not words:
            continue
        if words[0] == "git":
            if len(words) < 2 or words[1] not in READ_ONLY_GIT:
                return f"repo is read-only: 'git {words[1] if len(words) > 1 else ''}' not allowed"
        elif words[0] not in READ_ONLY_COMMANDS:
            return f"repo is read-only: '{words[0]}' not allowed"
        elif words[0] == "find" and any(w in ("-delete", "-exec", "-execdir") for w in words):
            return "repo is read-only: find -delete/-exec not allowed"
    return None


def _owning_root(path: Path, roots: list[Repo]) -> Repo | None:
    for root in roots:
        if path == root.path or root.path in path.parents:
            return root
    return None


def check_tool_use(
    tool_name: str, tool_input: dict[str, Any], roots: list[Repo], cwd: Path
) -> Decision | None:
    """Return a deny/ask decision, or None if the call is allowed outright."""
    if tool_name == "PowerShell":
        return Decision("deny", "use Bash; PowerShell bypasses the command guard")
    if tool_name in PATH_TOOLS:
        raw = next((tool_input[k] for k in PATH_KEYS if tool_input.get(k)), None)
        if raw is None:
            return None  # Glob/Grep without a path search the cwd, which is a root
        path = Path(raw)
        path = (path if path.is_absolute() else cwd / path).resolve()
        root = _owning_root(path, roots)
        if root is None:
            return Decision("deny", f"{path} is outside the delegated repos")
        if tool_name in WRITE_TOOLS and root.access is not Access.WRITE:
            return Decision("deny", f"repo '{root.name}' is read-only")
        if tool_name in WRITE_TOOLS and path in PROTECTED_FILES:
            return Decision("ask", PROTECTED_FILES[path])
    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        for pattern, label in BLOCKED_COMMANDS:
            if pattern.search(command):
                return Decision("deny", f"blocked: {label}")
        root = _owning_root(cwd.resolve(), roots)
        writes = _read_only_violation(command)
        if writes and (root is None or root.access is not Access.WRITE):
            return Decision("deny", writes)
        targets_self = cwd.resolve() == PROJECT_ROOT.resolve() or PROJECT_ROOT.name in command
        if writes and targets_self and any(p.name in command for p in PROTECTED_FILES):
            return Decision("ask", "run a command that may modify the agent's own config or guard")
        for pattern, label in ASK_COMMANDS:
            if pattern.search(command):
                return Decision("ask", label)
    return None


Approver = Callable[[str, dict[str, Any], str], Awaitable[bool]]


def _decision(kind: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def guard_hook(roots: list[Repo], cwd: Path, approve: Approver) -> HookMatcher:
    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext):
        tool, args = input_data["tool_name"], input_data["tool_input"]
        decision = check_tool_use(tool, args, roots, cwd)
        if decision is None:
            return {}
        if decision.action == "ask":
            if await approve(tool, args, decision.reason):
                return _decision("allow", f"user approved: {decision.reason}")
            return _decision("deny", f"user declined: {decision.reason}")
        return _decision("deny", decision.reason)

    # Long timeout: approval waits on a human.
    return HookMatcher(matcher=None, hooks=[hook], timeout=900)

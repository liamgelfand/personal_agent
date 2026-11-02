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
]

# Merging always needs the user, even with auto_approve (the API route skips `gh pr merge`).
MERGE_COMMANDS = [
    (re.compile(r"\bgh\s+pr\s+merge\b"), "merge a PR"),
    (re.compile(r"\bgh\s+api\b.*/pulls/\d+/merge\b"), "merge a PR"),
]

# Allowed only after the user approves in the UI (or automatically with auto_approve).
ASK_COMMANDS = [
    (re.compile(r"\bgit\s+push\b"), "push to a remote"),
    (re.compile(r"\bgit\s+commit\b"), "create a commit"),
    (re.compile(r"\bgh\s+(pr|issue|release)\s+(create|edit|close|comment|review)\b"), "write to GitHub"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+(install|i|add|publish)\b"), "install or publish packages"),
    (re.compile(r"\b(pip|pip3)\s+install\b|\buv\s+(add|pip\s+install)\b"), "install packages"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(ba|z)?sh\b"), "run a downloaded script"),
    (re.compile(r"\bdocker\b"), "run docker"),
]

# `git -C dir push`, `git -c k=v commit`, `"git" push`: drop git's global options and quotes
# before matching, so the patterns above see `git push` / `git commit`.
GIT_GLOBAL_OPTIONS = re.compile(
    r"""(?<![\w./\\-])["']?git(?:\.exe)?["']?"""
    r"""((?:\s+(?:-[Cc]\s+(?:"[^"]*"|'[^']*'|\S+)|--[\w-]+(?:=\S+)?|-[A-Za-z]))*)(?=\s)"""
)

# The agent may work on its own repo, but changing what it is allowed to do needs your approval.
# Only the live checkout is protected; copies in task worktrees are reviewed before they land.
PROTECTED_FILES = {
    (PROJECT_ROOT / "config.yaml").resolve(): "edit the agent's repo access config",
    (PROJECT_ROOT / ".env").resolve(): "edit the agent's environment file",
    (PROJECT_ROOT / "src" / "personal_agent" / "guards.py").resolve(): "edit the agent's safety guard",
}
SHELL_WILDCARDS = re.compile(r"[*?\[\]{}$]")

# In read-only roots, Bash may only run these (every segment of a pipeline/chain).
READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "pwd", "echo",
    "which", "file", "stat", "du", "diff", "sort", "uniq", "cut", "less", "jq",
}
READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "branch", "rev-parse", "ls-files", "grep"}
# Options that make an otherwise read-only command write a file.
WRITING_OPTIONS = {
    "sort": ("-o", "--output"),
    "tree": ("-o",),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "rg": ("--pre",),
}
GIT_BRANCH_LISTING = {"-a", "-r", "-l", "-v", "-vv", "--all", "--remotes", "--list", "--show-current",
                      "--merged", "--no-merged", "--contains", "--no-color", "--color"}
HARMLESS_REDIRECTS = re.compile(r"\d?>&\d|[12]?>\s*/dev/null")
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|&\n]")


@dataclass
class Decision:
    action: Literal["deny", "ask"]
    reason: str
    always_ask: bool = False  # asks even when auto_approve is on


def normalize_command(command: str) -> str:
    return GIT_GLOBAL_OPTIONS.sub("git", command)


def _read_only_violation(command: str) -> str | None:
    """Why `command` might write, or None if every part of it is a known read-only command."""
    command = HARMLESS_REDIRECTS.sub("", normalize_command(command))
    if any(token in command for token in (">", "`", "$(", "<(")):
        return "repo is read-only: no redirection, process or command substitution"
    for segment in SEGMENT_SPLIT.split(command):
        words = [w.strip("\"'") for w in segment.split()]
        if not words:
            continue
        name = words[0]
        if name == "git":
            sub = words[1] if len(words) > 1 else ""
            if sub not in READ_ONLY_GIT:
                return f"repo is read-only: 'git {sub}' not allowed"
            if any(w.startswith("--output") for w in words):
                return "repo is read-only: git --output writes a file"
            if sub == "branch" and any(w.startswith("-") and w not in GIT_BRANCH_LISTING for w in words[2:]):
                return "repo is read-only: only listing branches is allowed"
            if sub == "branch" and any(not w.startswith("-") for w in words[2:]):
                return "repo is read-only: creating branches is not allowed"
        elif name not in READ_ONLY_COMMANDS:
            return f"repo is read-only: '{name}' not allowed"
        elif any(w == opt or w.startswith(opt + "=") for w in words for opt in WRITING_OPTIONS.get(name, ())):
            return f"repo is read-only: '{name}' with an option that writes files"
    return None


def _owning_root(path: Path, roots: list[Repo]) -> Repo | None:
    for root in roots:
        if path == root.path or root.path in path.parents:
            return root
    return None


def _glob_base(pattern: str, base: Path) -> Path:
    """The fixed directory a glob pattern starts from (everything before the first wildcard)."""
    parts = []
    for part in Path(pattern).parts:
        if any(ch in part for ch in "*?[{"):
            break
        parts.append(part)
    prefix = Path(*parts) if parts else Path(".")
    return (prefix if prefix.is_absolute() else base / prefix).resolve()


def _check_path(tool_name: str, path: Path, roots: list[Repo]) -> Decision | None:
    root = _owning_root(path, roots)
    if root is None:
        return Decision("deny", f"{path} is outside the delegated repos")
    if tool_name in WRITE_TOOLS and root.access is not Access.WRITE:
        return Decision("deny", f"repo '{root.name}' is read-only")
    if tool_name in WRITE_TOOLS and path in PROTECTED_FILES:
        return Decision("ask", PROTECTED_FILES[path])
    return None


def check_tool_use(
    tool_name: str, tool_input: dict[str, Any], roots: list[Repo], cwd: Path
) -> Decision | None:
    """Return a deny/ask decision, or None if the call is allowed outright."""
    if tool_name == "PowerShell":
        return Decision("deny", "use Bash; PowerShell bypasses the command guard")
    if tool_name in PATH_TOOLS:
        raw = next((tool_input[k] for k in PATH_KEYS if tool_input.get(k)), None)
        base = cwd
        if raw is not None:
            path = Path(raw)
            base = (path if path.is_absolute() else cwd / path).resolve()
            if decision := _check_path(tool_name, base, roots):
                return decision
        # Glob's pattern can itself be absolute or climb out with '..'.
        if tool_name == "Glob" and tool_input.get("pattern"):
            return _check_path(tool_name, _glob_base(tool_input["pattern"], base), roots)
    elif tool_name == "Bash":
        raw_command = tool_input.get("command", "")
        command = normalize_command(raw_command)
        for pattern, label in BLOCKED_COMMANDS:
            if pattern.search(command):
                return Decision("deny", f"blocked: {label}")
        root = _owning_root(cwd.resolve(), roots)
        writes = _read_only_violation(raw_command)
        if writes and (root is None or root.access is not Access.WRITE):
            return Decision("deny", writes)
        targets_self = cwd.resolve() == PROJECT_ROOT.resolve() or PROJECT_ROOT.name in command
        names_protected = any(p.name in command for p in PROTECTED_FILES)
        # Wildcards or variables could name a protected file without spelling it out.
        if writes and targets_self and (names_protected or SHELL_WILDCARDS.search(command)):
            return Decision("ask", "run a command that may modify the agent's own config or guard")
        for pattern, label in MERGE_COMMANDS:
            if pattern.search(command):
                return Decision("ask", label, always_ask=True)
        for pattern, label in ASK_COMMANDS:
            if pattern.search(command):
                return Decision("ask", label)
    return None


APPROVAL_DETAIL_LIMIT = 20_000


def approval_detail(tool_name: str, args: dict[str, Any]) -> str:
    """Everything the user needs to judge a call: the full command, or the path plus the new content."""
    if tool_name == "Bash":
        text = args.get("command", "")
    elif tool_name == "Write":
        text = f"{args.get('file_path', '')}\n\n{args.get('content', '')}"
    elif tool_name == "Edit":
        text = (f"{args.get('file_path', '')}\n\n--- replace\n{args.get('old_string', '')}"
                f"\n\n--- with\n{args.get('new_string', '')}")
    elif tool_name == "MultiEdit":
        edits = "\n\n".join(f"--- replace\n{e.get('old_string', '')}\n\n--- with\n{e.get('new_string', '')}"
                            for e in args.get("edits", []))
        text = f"{args.get('file_path', '')}\n\n{edits}"
    else:
        text = "\n".join(f"{k}: {v}" for k, v in args.items())
    if len(text) > APPROVAL_DETAIL_LIMIT:
        text = text[:APPROVAL_DETAIL_LIMIT] + f"\n\n[... {len(text) - APPROVAL_DETAIL_LIMIT} more characters]"
    return text


Approver = Callable[[str, dict[str, Any], str], Awaitable[bool]]


def _decision(kind: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": kind,
            "permissionDecisionReason": reason,
        }
    }


def guard_hook(roots: list[Repo], cwd: Path, approve: Approver, auto_approve: bool = False) -> HookMatcher:
    async def hook(input_data: dict[str, Any], tool_use_id: str | None, context: HookContext):
        tool, args = input_data["tool_name"], input_data["tool_input"]
        decision = check_tool_use(tool, args, roots, cwd)
        if decision is None:
            return {}
        if decision.action == "ask":
            if auto_approve and not decision.always_ask:
                return _decision("allow", f"auto-approved: {decision.reason}")
            if await approve(tool, args, decision.reason):
                return _decision("allow", f"user approved: {decision.reason}")
            return _decision("deny", f"user declined: {decision.reason}")
        return _decision("deny", decision.reason)

    # Long timeout: approval waits on a human.
    return HookMatcher(matcher=None, hooks=[hook], timeout=900)

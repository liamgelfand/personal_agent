"""Runs one agent query and normalizes SDK messages into UI events. Every agent run goes through here."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    query,
)

from .bus import Bus
from .store import Store

SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "query", "url", "description", "title")


async def _messages(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
    """SDK messages, ending quietly after an error result.

    When Claude Code ends with an error result (turn limit, usage limit, API error) the SDK yields
    the ResultMessage and then raises ResultError. The result already carries the details.
    """
    got_error_result = False
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage) and message.is_error:
                got_error_result = True
            yield message
    except ResultError:
        if not got_error_result:
            raise


def summarize_tool(name: str, args: dict[str, Any]) -> str:
    for key in SUMMARY_KEYS:
        if value := args.get(key):
            return str(value)[:300]
    return ""


async def run_agent(
    options: ClaudeAgentOptions, prompt: str, store: Store, bus: Bus, kind: str, ref: str | None
) -> AsyncIterator[dict[str, Any]]:
    """Yield events: delta, text, tool, result. Records cost; pushes rate limits to the bus."""
    run_id = store.start_run(kind, ref)
    result: ResultMessage | None = None
    try:
        async for message in _messages(prompt, options):
            if isinstance(message, StreamEvent):
                event = message.event
                delta = event.get("delta", {})
                if (
                    message.parent_tool_use_id is None
                    and event.get("type") == "content_block_delta"
                    and delta.get("type") == "text_delta"
                ):
                    yield {"type": "delta", "text": delta["text"]}
            elif isinstance(message, AssistantMessage):
                agent = "subagent" if message.parent_tool_use_id else "main"
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "agent": agent, "text": block.text}
                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool",
                            "agent": agent,
                            "name": block.name,
                            "summary": summarize_tool(block.name, block.input),
                            "subagent": block.input.get("subagent_type"),
                        }
            elif isinstance(message, RateLimitEvent):
                windows = (message.rate_limit_info.raw or {}).get("unifiedWindows")
                if windows:
                    bus.rate_limits = windows
                    await bus.publish({"type": "rate_limit", "windows": windows})
            elif isinstance(message, ResultMessage):
                result = message
                yield {
                    "type": "result",
                    "is_error": message.is_error,
                    "num_turns": message.num_turns,
                    "cost_usd": message.total_cost_usd,
                    "errors": message.errors,
                    "session_id": message.session_id,
                    "structured_output": message.structured_output,
                    "result": message.result,
                }
    finally:
        store.finish_run(
            run_id,
            result.num_turns if result else 0,
            result.total_cost_usd if result else None,
            result.is_error if result else True,
        )

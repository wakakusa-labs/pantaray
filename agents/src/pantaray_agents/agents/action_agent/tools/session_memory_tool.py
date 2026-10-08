"""write_session_memory tool definition."""

from __future__ import annotations

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ToolConcurrency

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

WRITE_SESSION_MEMORY_TOOL_ID = "write_session_memory"
# The notes ride in the conversation as call arguments, never omitted, so every
# later turn pays for them; 8 KiB keeps that cost bounded.
SESSION_MEMORY_MAX_BYTES = 8_192

WRITE_SESSION_MEMORY_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=WRITE_SESSION_MEMORY_TOOL_ID,
        name="Write Session Memory",
        description="Replace your notes for this conversation.",
        guide=ToolGuideSpec(
            what=(
                "Replaces your session memory: notes for this conversation only. "
                "Your latest successful call is the current session memory, and "
                "its content stays visible in the conversation even when older "
                "tool results are omitted."
            ),
            when=(
                "Use on your own initiative to note what you are likely to need "
                "again later in this conversation, and to update notes that "
                "became stale."
            ),
            pitfalls=(
                "Each call replaces the whole session memory, so send the complete "
                f"notes. The limit is {SESSION_MEMORY_MAX_BYTES:,} UTF-8 bytes; a "
                "larger write is rejected "
                "and the previous notes stay current. This is not long-term "
                "memory. Do not record secrets such as passwords, API keys, or "
                "tokens."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="surgical_edit",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="content",
                    schema={"type": "string"},
                    required=True,
                    description=(
                        "The complete session memory in Markdown or plain text, "
                        f"at most {SESSION_MEMORY_MAX_BYTES:,} UTF-8 bytes."
                    ),
                ),
            ),
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "const": "written"},
                "bytes": {"type": "integer"},
                "limit_bytes": {"type": "integer"},
            },
            "required": ["status", "bytes", "limit_bytes"],
            "additionalProperties": False,
        },
    )
)


def write_session_memory(content: str) -> dict[str, JSONValue]:
    """The result of writing ``content``, which nothing stores: the conversation
    keeps the call. ``ValueError`` with the model-facing message when too large.
    """

    size = len(content.encode("utf-8"))
    if size > SESSION_MEMORY_MAX_BYTES:
        raise ValueError(
            f"Session memory is {size:,} UTF-8 bytes, over the limit of "
            f"{SESSION_MEMORY_MAX_BYTES:,} bytes. Nothing was written; the "
            "previous session memory is still current. Shorten it and write again."
        )
    return {"status": "written", "bytes": size, "limit_bytes": SESSION_MEMORY_MAX_BYTES}


__all__ = [
    "SESSION_MEMORY_MAX_BYTES",
    "WRITE_SESSION_MEMORY_TOOL",
    "WRITE_SESSION_MEMORY_TOOL_ID",
    "write_session_memory",
]

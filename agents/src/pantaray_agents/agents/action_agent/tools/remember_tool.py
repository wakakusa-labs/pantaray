"""remember tool definition."""

from __future__ import annotations

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

REMEMBER_TOOL_ID = "remember"
# A note is one request with its context, not a document to archive.
_NOTE_MAX_LENGTH = 2000

REMEMBER_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=REMEMBER_TOOL_ID,
        name="Remember",
        description=(
            "Record a user's explicit request to remember, forget, or update "
            "something as a note in memory."
        ),
        guide=ToolGuideSpec(
            what=(
                "Appends one short note to memory. memory_search finds it right "
                "away, in this Action and later ones. It does not edit existing "
                "memory."
            ),
            when=(
                "Use only when the user explicitly asks you to remember, forget, "
                "or update something, for example 覚えておいて or これは忘れて. "
                "Write the request in the user's own words, plus the context "
                "needed to understand it later."
            ),
            pitfalls=(
                "Never tell the user that something is remembered, forgotten, or "
                "updated without calling this tool. Do not call it unless the "
                "user asked. Do not record secrets such as passwords, API keys, "
                "or tokens."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="surgical_edit",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="note",
                    schema={
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _NOTE_MAX_LENGTH,
                    },
                    required=True,
                    description=(
                        "The user's request in their own words, with the context "
                        "needed to understand it later."
                    ),
                ),
            )
        ),
        output_schema={
            "type": "object",
            "properties": {"status": {"type": "string", "const": "remembered"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )
)

__all__ = ["REMEMBER_TOOL", "REMEMBER_TOOL_ID"]

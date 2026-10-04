"""history_fetch ツール定義。"""

from __future__ import annotations

from pantaray_agents.schema.agent.action_history import (
    HISTORY_FETCH_MAX_REFS,
    HISTORY_FETCH_SHORT_STEP_PATTERN,
    history_fetch_refs_schema,
)

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

HISTORY_FETCH_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="history_fetch",
        name="History Fetch",
        description=(
            "Fetch selected persisted Action history steps by their displayed short "
            "step IDs without rerunning any tool."
        ),
        guide=ToolGuideSpec(
            what=(
                "Retrieve the exact stored content of selected USER, ASSISTANT, THINK, and TOOL "
                "history entries as inline pages of a JSON document."
            ),
            when=(
                "Use when compressed history omits details needed for the current "
                "decision."
            ),
            pitfalls=(
                "Use only refs shown in history, request only relevant steps, and do "
                "not derive one suffix from another. Continue with the same ordered refs "
                "and the returned next_cursor until it is null. content is a JSON "
                "text fragment; concatenate pages in offset order. If a ref changes "
                "during a retry, the cursor is rejected: restart without a cursor."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="refs",
                    schema=history_fetch_refs_schema(),
                    required=True,
                ),
                field_spec(
                    name="cursor",
                    schema={
                        "type": ["string", "null"],
                        "pattern": "^[0-9a-f]{64}:[0-9]+$",
                        # SHA-256 hex, a colon, and at most 20 offset digits.
                        "maxLength": 85,
                    },
                    required=False,
                    description="Use next_cursor from the preceding page with the same refs; omit for the first page.",
                ),
            )
        ),
        output_schema={
            "type": "object",
            "properties": {
                "refs": history_fetch_refs_schema(),
                "content": {
                    "type": "string",
                    "description": "A fragment of the selected steps serialized as JSON, including stored tool outputs.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based character offset in the JSON document.",
                },
                "total_characters": {"type": "integer", "minimum": 1},
                "next_cursor": {"type": ["string", "null"]},
            },
            "required": [
                "refs",
                "content",
                "offset",
                "total_characters",
                "next_cursor",
            ],
            "additionalProperties": False,
        },
    )
)


__all__ = [
    "HISTORY_FETCH_MAX_REFS",
    "HISTORY_FETCH_SHORT_STEP_PATTERN",
    "HISTORY_FETCH_TOOL",
]

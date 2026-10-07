"""Final-answer submit tool definition."""

from __future__ import annotations

from pantaray_agents.tools.contract import ToolConcurrency

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)

SUBMIT_FINAL_ANSWER_TOOL_ID = "submit_final_answer"


SUBMIT_FINAL_ANSWER_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=SUBMIT_FINAL_ANSWER_TOOL_ID,
        name="Submit Final Answer",
        description="Finalize the stored Supervisor answer draft.",
        guide=ToolGuideSpec(
            what=(
                "Use this to finalize the stored Supervisor answer draft as the "
                "user-facing final answer."
            ),
            when=(
                "Use when the stored draft is already the right final response "
                "for the user."
            ),
            pitfalls=(
                "Do not pass answer text to this tool. If the answer text needs "
                "changes, call `draft_final_answer` again before submitting."
            ),
        ),
        # Run-ending: it settles the Action's status, so a sibling after it would
        # write to a finished Action.
        concurrency=ToolConcurrency("run_ending"),
        execution_policy=tool_execution_policy(
            intent_class="bulk_edit",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(),
            description="Finalize the stored Supervisor answer draft.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["final_answer_submitted"]},
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    )
)


__all__ = [
    "SUBMIT_FINAL_ANSWER_TOOL",
    "SUBMIT_FINAL_ANSWER_TOOL_ID",
]

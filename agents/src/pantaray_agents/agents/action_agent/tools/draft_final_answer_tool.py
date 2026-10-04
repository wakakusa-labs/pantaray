"""Final-answer draft tool definition."""

from __future__ import annotations

from collections.abc import Mapping

from pantaray_agents.schema.agent.base import JSONValue

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolPolicyValidationError,
    ToolSpec,
    ToolValidationDetails,
    field_spec,
    tool_execution_policy,
)

DRAFT_FINAL_ANSWER_TOOL_ID = "draft_final_answer"

_NON_EMPTY_STRING_SCHEMA = {
    "type": "string",
    "minLength": 1,
}


def _validate_draft_final_answer_args(args: Mapping[str, JSONValue]) -> None:
    answer = args.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        details: ToolValidationDetails = {
            "path": ["args", "answer"],
            "message": "answer must be a non-empty string after trimming whitespace.",
        }
        raise ToolPolicyValidationError(
            "answer must be a non-empty string after trimming whitespace.",
            details=details,
        )


DRAFT_FINAL_ANSWER_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=DRAFT_FINAL_ANSWER_TOOL_ID,
        name="Draft Final Answer",
        description=(
            "Create or replace the pending final-answer draft for the current "
            "execution scope."
        ),
        guide=ToolGuideSpec(
            what=(
                "Use this to store or revise the answer text that may later be "
                "finalized for the current execution scope. This tool does not "
                "complete the scope."
            ),
            when=(
                "Use when the final-answer candidate is ready to be saved as a "
                "draft. Calling this tool again replaces the previous draft."
            ),
            pitfalls=(
                "Do not use this for notes, reasoning, partial work, or a record "
                "of what to do next. The answer must be deliverable text for the "
                "current scope."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="bulk_edit",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="answer",
                    schema=_NON_EMPTY_STRING_SCHEMA,
                    required=True,
                    description=(
                        "Final-answer draft to store for the current execution "
                        "scope. Calling this again replaces the previous draft."
                    ),
                ),
            ),
            description=(
                "Store a pending final-answer draft for the current execution scope."
            ),
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["draft_updated"]},
                "next_step": {"type": "string"},
                "draft_revision": {"type": "string", "pattern": "^sha256:"},
            },
            "required": ["status", "next_step", "draft_revision"],
            "additionalProperties": False,
        },
        pre_validate_args=_validate_draft_final_answer_args,
    )
)

__all__ = [
    "DRAFT_FINAL_ANSWER_TOOL",
    "DRAFT_FINAL_ANSWER_TOOL_ID",
]

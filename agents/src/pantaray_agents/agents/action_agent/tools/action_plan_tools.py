"""Parent-only soft Action plan tools."""

from __future__ import annotations

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

READ_ACTION_PLAN_TOOL_ID = "read_action_plan"
WRITE_ACTION_PLAN_TOOL_ID = "write_action_plan"

READ_ACTION_PLAN_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=READ_ACTION_PLAN_TOOL_ID,
        name="Read Action Plan",
        description="Read the optional soft plan for this Action.",
        guide=ToolGuideSpec(
            what="Read this Action's app-managed plan.md when it exists.",
            when="Use when resuming or reconciling a complex Action that has a plan.",
            pitfalls=(
                "Simple Actions may have no plan. The path is fixed and cannot be "
                "used to read project files."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(description="Read this Action's optional plan.md."),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["absent", "present"]},
                "content": {"type": "string"},
            },
            "required": ["status"],
            "additionalProperties": False,
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "present"}}},
                    "then": {"required": ["content"]},
                    "else": {"not": {"required": ["content"]}},
                }
            ],
        },
    )
)

WRITE_ACTION_PLAN_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=WRITE_ACTION_PLAN_TOOL_ID,
        name="Write Action Plan",
        description="Create or atomically replace the soft plan for this Action.",
        guide=ToolGuideSpec(
            what="Write complete plan.md; another call replaces the prior document.",
            when=(
                "Use for complex, long-running, uncertain, or multi-workstream work. "
                "Direct work does not need a plan."
            ),
            pitfalls="Send the whole document. The fixed path cannot edit project files.",
        ),
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
                    description="Complete bounded UTF-8 Markdown document.",
                ),
            ),
            description="Atomically replace this Action's canonical plan.md.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["written"]},
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    )
)

__all__ = [
    "READ_ACTION_PLAN_TOOL",
    "READ_ACTION_PLAN_TOOL_ID",
    "WRITE_ACTION_PLAN_TOOL",
    "WRITE_ACTION_PLAN_TOOL_ID",
]

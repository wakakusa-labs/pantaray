"""Supervisor subagent spawn tool contract."""

from __future__ import annotations

from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ACTION_SUBAGENT_MESSAGE_ID_MAX_CODEPOINTS,
    ACTION_SUBAGENT_MESSAGE_MAX_CODEPOINTS,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    ACTION_SUBAGENT_REF_LIST_MAX_ITEMS,
    ACTION_SUBAGENT_TASK_MAX_CODE_POINTS,
)
from pantaray_agents.schema.agent.action_subagent import (
    ACTION_SUBAGENT_MAX_ACTIVE_CHILDREN_PER_PARENT,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

from .base import (
    FieldSpec,
    InputSpec,
    SchemaMapping,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

SPAWN_SUBAGENT_TOOL_ID = "spawn_subagent"
SEND_MESSAGE_TO_SUBAGENT_TOOL_ID = "send_message_to_subagent"
WAIT_SUBAGENTS_TOOL_ID = "wait_subagents"
CANCEL_SUBAGENT_TOOL_ID = "cancel_subagent"
# The subagent's own terminal tool; the child job defines its contract.
SUBMIT_SUBAGENT_REPORT_TOOL_ID = "submit_subagent_report"
_MODEL_GUIDANCE = " ".join(
    f"{setting.selector}: {setting.recommendation}"
    for setting in SUBAGENT_MODEL_SETTINGS
)
_NON_BLANK_STRING: SchemaMapping = {
    "type": "string",
    "minLength": 1,
    "pattern": r".*\S.*",
}
_RESOURCE_CLAIM_SCHEMA: SchemaMapping = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "kind": {"const": "workspace_path"},
                "path": _NON_BLANK_STRING,
            },
            "required": ["kind", "path"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "kind": {"const": "external_resource"},
                "root_identity": _NON_BLANK_STRING,
                "normalized_key": _NON_BLANK_STRING,
            },
            "required": ["kind", "root_identity", "normalized_key"],
            "additionalProperties": False,
        },
    ]
}


def _wait_result_schema(
    status: str,
    extra: tuple[tuple[str, dict[str, JSONValue]], ...] = (),
) -> dict[str, JSONValue]:
    properties: dict[str, JSONValue] = {
        "child_process_id": {"type": "string"},
        "status": {"const": status},
        **dict(extra),
    }
    return {
        "type": "object",
        "properties": properties,
        "required": ["child_process_id", "status", *(name for name, _ in extra)],
        "additionalProperties": False,
    }


def _wait_result_union_schema() -> dict[str, JSONValue]:
    return {
        "oneOf": [
            _wait_result_schema("success", (("report", {"type": "string"}),)),
            _wait_result_schema("failure", (("error_code", {"type": "string"}),)),
            _wait_result_schema("canceled"),
            _wait_result_schema("nonterminal"),
        ]
    }


def _required_field(
    name: str,
    schema: SchemaMapping,
    description: str,
) -> FieldSpec:
    return field_spec(
        name=name,
        schema=schema,
        required=True,
        description=description,
    )


SPAWN_SUBAGENT_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=SPAWN_SUBAGENT_TOOL_ID,
        name="Spawn Subagent",
        description=(
            "Start one bounded subagent job with an explicit configured model and "
            "resource claims. " + _MODEL_GUIDANCE
        ),
        guide=ToolGuideSpec(
            what="Delegate one bounded task to an independently executing subagent.",
            when=_MODEL_GUIDANCE,
            pitfalls=(
                "Claim every resource the child may write. Replaying the same decision "
                "with changed content is rejected."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                _required_field(
                    "model",
                    {
                        "type": "string",
                        "enum": [
                            setting.selector for setting in SUBAGENT_MODEL_SETTINGS
                        ],
                    },
                    _MODEL_GUIDANCE,
                ),
                _required_field(
                    "task",
                    {
                        **_NON_BLANK_STRING,
                        "maxLength": ACTION_SUBAGENT_TASK_MAX_CODE_POINTS,
                    },
                    "Complete bounded task delegated to the subagent.",
                ),
                _required_field(
                    "context_refs",
                    {
                        "type": "array",
                        "items": _NON_BLANK_STRING,
                        "maxItems": ACTION_SUBAGENT_REF_LIST_MAX_ITEMS,
                    },
                    "Opaque durable context references for the child.",
                ),
                _required_field(
                    "resource_claims",
                    {
                        "type": "array",
                        "items": _RESOURCE_CLAIM_SCHEMA,
                        "maxItems": ACTION_SUBAGENT_REF_LIST_MAX_ITEMS,
                    },
                    "Exact workspace or external resources the child may write.",
                ),
            ),
            description="Spawn one configured subagent from the current Supervisor THINK.",
        ),
        output_schema={
            "type": "object",
            "properties": {"child_process_id": {"type": "string"}},
            "required": ["child_process_id"],
            "additionalProperties": False,
        },
    )
)

SEND_MESSAGE_TO_SUBAGENT_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
        name="Send Message to Subagent",
        description="Durably send one bounded private message to an active subagent.",
        guide=ToolGuideSpec(
            what="Send correction or additional context to one owned subagent.",
            when="Use when the child should consider new information on its next turn.",
            pitfalls=(
                "Reuse a message_id only for an exact retry of the same content. "
                "Acceptance does not mean the child has consumed the message."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                _required_field(
                    "child_process_id",
                    _NON_BLANK_STRING,
                    "Exact child process identity returned by spawn_subagent.",
                ),
                _required_field(
                    "message_id",
                    {
                        **_NON_BLANK_STRING,
                        "maxLength": ACTION_SUBAGENT_MESSAGE_ID_MAX_CODEPOINTS,
                    },
                    "Stable identity for exact retry of this message.",
                ),
                _required_field(
                    "content",
                    {
                        **_NON_BLANK_STRING,
                        "maxLength": ACTION_SUBAGENT_MESSAGE_MAX_CODEPOINTS,
                    },
                    "Private correction or additional context for the child.",
                ),
            ),
            description="Durably accept one private parent-to-child message.",
        ),
        output_schema={"const": True},
    )
)

WAIT_SUBAGENTS_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=WAIT_SUBAGENTS_TOOL_ID,
        name="Wait for Subagents",
        description=(
            "Wait up to 30 seconds for named owned subagents and return each "
            "terminal result or a named nonterminal result."
        ),
        guide=ToolGuideSpec(
            what="Collect terminal reports from previously spawned subagents.",
            when="Use after spawning work that the parent needs before continuing.",
            pitfalls=(
                "Pass only child process identities returned to this parent. "
                "A nonterminal result may be waited on again."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                _required_field(
                    "child_process_ids",
                    {
                        "type": "array",
                        "items": _NON_BLANK_STRING,
                        "minItems": 1,
                        "maxItems": ACTION_SUBAGENT_MAX_ACTIVE_CHILDREN_PER_PARENT,
                        "uniqueItems": True,
                    },
                    "One to four exact child process identities owned by this parent.",
                ),
            ),
            description="Wait for a bounded set of this parent's subagents.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": ACTION_SUBAGENT_MAX_ACTIVE_CHILDREN_PER_PARENT,
                    "items": _wait_result_union_schema(),
                }
            },
            "required": ["results"],
            "additionalProperties": False,
        },
    )
)

CANCEL_SUBAGENT_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=CANCEL_SUBAGENT_TOOL_ID,
        name="Cancel Subagent",
        description=(
            "Cancel one owned subagent: queued work stops immediately and running "
            "work at a safe boundary. Waits 30 seconds; retry a nonterminal result."
        ),
        guide=ToolGuideSpec(
            what="Cancel and collect one previously spawned subagent.",
            when="Use when the parent no longer needs the child's work.",
            pitfalls="Use the exact owned child ID; retry a nonterminal result.",
        ),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=30_000,
        ),
        input_spec=InputSpec(
            fields=(
                _required_field(
                    "child_process_id",
                    _NON_BLANK_STRING,
                    "Exact child process identity owned by this parent.",
                ),
            ),
            description="Request cancellation and observe one owned subagent.",
        ),
        output_schema=_wait_result_union_schema(),
    )
)


__all__ = [
    "CANCEL_SUBAGENT_TOOL",
    "CANCEL_SUBAGENT_TOOL_ID",
    "SEND_MESSAGE_TO_SUBAGENT_TOOL",
    "SEND_MESSAGE_TO_SUBAGENT_TOOL_ID",
    "SPAWN_SUBAGENT_TOOL",
    "SPAWN_SUBAGENT_TOOL_ID",
    "WAIT_SUBAGENTS_TOOL",
    "WAIT_SUBAGENTS_TOOL_ID",
]

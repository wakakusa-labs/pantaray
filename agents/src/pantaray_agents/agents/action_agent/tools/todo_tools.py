"""update_todo and request_todo_deletion: what an Action says about its own TODO.

Offered only to the Action whose conversation is a TODO's, and always about that
TODO, so neither takes a TODO id. A refusal is an ordinary result the model
reads, not a failed call.
"""

from __future__ import annotations

from typing import Final, Literal

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

UPDATE_TODO_TOOL_ID = "update_todo"
REQUEST_TODO_DELETION_TOOL_ID = "request_todo_deletion"
TODO_TOOL_IDS: Final = (UPDATE_TODO_TOOL_ID, REQUEST_TODO_DELETION_TOOL_ID)

type TodoStatus = Literal["done", "partial", "needs_user"]

# The note is one line under the TODO in the list; the reason is shown beside
# the user's choice to delete or keep it.
_NOTE_MAX_LENGTH = 200
_REASON_MAX_LENGTH = 400

# What the model reads when the TODO cannot take the call.
TODO_REFUSALS: Final[dict[str, str]] = {
    "deleted": "This TODO is deleted, so it cannot change.",
    "not_found": "This TODO no longer exists.",
    "already_pending": (
        "A deletion request for this TODO is already waiting for the user's answer."
    ),
    "declined_before": (
        "The user chose to keep this TODO, so its deletion cannot be requested again."
    ),
}


def _status_output(*statuses: str) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": [*statuses, "refused"]},
            "reason": {"type": "string", "enum": list(TODO_REFUSALS)},
            "message": {"type": "string"},
        },
        "required": ["status"],
        "additionalProperties": False,
    }


UPDATE_TODO_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=UPDATE_TODO_TOOL_ID,
        name="Update TODO",
        description="Report how far this conversation's TODO has come.",
        guide=ToolGuideSpec(
            what=(
                "Updates the TODO this conversation works on, as the user's TODO "
                "list shows it. done checks it off; partial and needs_user show "
                "the note under it."
            ),
            when=(
                "Use done only when the TODO's outcome is achieved and verified, "
                "partial when some of it is left, and needs_user when you stop "
                "for the user's reply or decision."
            ),
            pitfalls=(
                "The note is one short line in the user's language, saying what "
                "is left or what you need from them; the reply carries the "
                "details. A check cannot be undone by you, only by the user."
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
                    name="status",
                    schema={
                        "type": "string",
                        "enum": ["done", "partial", "needs_user"],
                    },
                    required=True,
                    description="Where the TODO stands now.",
                ),
                field_spec(
                    name="note",
                    schema={
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _NOTE_MAX_LENGTH,
                    },
                    required=True,
                    description=(
                        "One short line for the TODO list; not shown for done."
                    ),
                ),
            )
        ),
        output_schema=_status_output("checked", "reported"),
    )
)

REQUEST_TODO_DELETION_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=REQUEST_TODO_DELETION_TOOL_ID,
        name="Request TODO Deletion",
        description="Ask the user to delete this conversation's TODO.",
        guide=ToolGuideSpec(
            what=(
                "Shows the user your request to delete the TODO this conversation "
                "works on, with your reason. Only the user's approval deletes it."
            ),
            when=(
                "Use when the TODO no longer needs doing, for example it was "
                "already done elsewhere or the plan behind it changed."
            ),
            pitfalls=(
                "Write the reason for the user in one or two sentences in their "
                "language. Once the user chose to keep the TODO, it cannot be "
                "requested again."
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
                    name="reason",
                    schema={
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _REASON_MAX_LENGTH,
                    },
                    required=True,
                    description="Why the TODO can go, shown to the user.",
                ),
            )
        ),
        output_schema=_status_output("requested"),
    )
)


__all__ = [
    "REQUEST_TODO_DELETION_TOOL",
    "REQUEST_TODO_DELETION_TOOL_ID",
    "TODO_REFUSALS",
    "TODO_TOOL_IDS",
    "TodoStatus",
    "UPDATE_TODO_TOOL",
    "UPDATE_TODO_TOOL_ID",
]

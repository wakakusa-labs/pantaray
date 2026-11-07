"""update_todo, request_todo_deletion and record_progress: what an Action says
about its own TODO.

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
RECORD_PROGRESS_TOOL_ID = "record_progress"
TODO_TOOL_IDS: Final = (
    UPDATE_TODO_TOOL_ID,
    REQUEST_TODO_DELETION_TOOL_ID,
    RECORD_PROGRESS_TOOL_ID,
)

type TodoStatus = Literal["done", "partial", "needs_user"]

# The note is one line under the TODO in the list; the reason is shown beside
# the user's choice to delete or keep it.
_NOTE_MAX_LENGTH = 200
_REASON_MAX_LENGTH = 400
_UNIT_MAX_LENGTH = 20
_EVIDENCE_MAX_ITEMS = 10

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
    "evidence_not_in_run": (
        "Each evidence id must be the History Ref of a successful tool call in "
        "this run. Nothing was recorded."
    ),
    "invalid_text": "The unit and the note must not be blank. Nothing was recorded.",
    "record_conflict": "This call already recorded different progress.",
}

ALREADY_RECORDED_MESSAGE: Final = (
    "This figure is already recorded from the same tool results, so it was not "
    "added again."
)


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

RECORD_PROGRESS_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=RECORD_PROGRESS_TOOL_ID,
        name="Record Progress",
        description="Record a figure this conversation's TODO achieved.",
        guide=ToolGuideSpec(
            what=(
                "Adds a progress record to the TODO this conversation works on: a "
                "quantity and unit, a note, and the tool results that show the "
                "figure. Progress toward the user's goal is the sum of these "
                "records."
            ),
            when=(
                "Use when a tool result in this run shows a concrete figure the "
                "TODO achieved, such as a signed contract's amount or the number "
                "of items sent. Record each figure once."
            ),
            pitfalls=(
                "Never estimate, convert or compute a figure the results do not "
                "show. Give the quantity in the base unit, without prefixes such "
                "as 万 or k (30000000 with 円, not 3000 with 万円). Cite the History "
                "Refs shown with those results; refs that are not successful tool "
                "calls of this run are refused. Only the user can remove a record."
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
                    name="quantity",
                    schema={"type": "number", "exclusiveMinimum": 0},
                    required=True,
                    description="How much, as the results show it.",
                ),
                field_spec(
                    name="unit",
                    schema={
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _UNIT_MAX_LENGTH,
                    },
                    required=True,
                    description="The unit of the quantity, such as 円 or 件.",
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
                        "One short line for the user saying what the figure is."
                    ),
                ),
                field_spec(
                    name="evidence_step_ids",
                    schema={
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "maxItems": _EVIDENCE_MAX_ITEMS,
                        "uniqueItems": True,
                    },
                    required=True,
                    description=(
                        "History Refs of this run's tool results that show the figure."
                    ),
                ),
            )
        ),
        output_schema=_status_output("recorded", "already_recorded"),
    )
)


__all__ = [
    "ALREADY_RECORDED_MESSAGE",
    "RECORD_PROGRESS_TOOL",
    "RECORD_PROGRESS_TOOL_ID",
    "REQUEST_TODO_DELETION_TOOL",
    "REQUEST_TODO_DELETION_TOOL_ID",
    "TODO_REFUSALS",
    "TODO_TOOL_IDS",
    "TodoStatus",
    "UPDATE_TODO_TOOL",
    "UPDATE_TODO_TOOL_ID",
]

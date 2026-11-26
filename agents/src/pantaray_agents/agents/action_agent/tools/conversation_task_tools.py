"""The tools an Action schedules later turns of its own conversation with.

A scheduled turn sends a prompt to this conversation at a time, as if the
user sent it then. Each schedule or change asks the user; the tools reach only
the schedules this conversation made.
"""

from __future__ import annotations

from typing import Final

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

SCHEDULE_CONTINUATION_TOOL_ID = "schedule_continuation"
LIST_SCHEDULED_CONTINUATIONS_TOOL_ID = "list_scheduled_continuations"
STOP_SCHEDULED_CONTINUATION_TOOL_ID = "stop_scheduled_continuation"
CONVERSATION_TASK_TOOL_IDS: Final = (
    SCHEDULE_CONTINUATION_TOOL_ID,
    LIST_SCHEDULED_CONTINUATIONS_TOOL_ID,
    STOP_SCHEDULED_CONTINUATION_TOOL_ID,
)

_INSTANT: Final[dict[str, JSONValue]] = {
    "type": ["string", "null"],
    "format": "date-time",
    "minLength": 1,
}
_DURATION_PATTERN: Final = r"^[0-9]+[smhd]$"
_TASK_ID: Final[dict[str, JSONValue]] = {
    "type": "string",
    "pattern": r"^task\.[a-z0-9][a-z0-9_-]{0,63}$",
}

SCHEDULE_CONTINUATION_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=SCHEDULE_CONTINUATION_TOOL_ID,
        name="Schedule Continuation",
        description=(
            "Schedule this conversation to continue later with a prompt, once or "
            "repeatedly."
        ),
        guide=ToolGuideSpec(
            what=(
                "At each scheduled time Pantaray sends the prompt to this "
                "conversation as its next turn, as if the user sent it then, and "
                "the turn runs with this conversation's history. Give at for one "
                "time, or every to repeat. every is a calendar unit (hour, day, "
                "week, month, quarter) that fires at the unit's start on local time "
                "plus offset, or a duration (30m, 2h, 3d) that repeats from the "
                "approval. Weeks start on Monday 00:00 and months on the 1st, so "
                'every day at 08:30 is every "day" with offset "510m", and Mondays '
                'at 09:00 is every "week" with offset "9h". A schedule never fires '
                "at or after until. Give task_id to replace one of this "
                "conversation's schedules; it then starts afresh."
            ),
            when=(
                "Use when the user asks for work at a later time or on a "
                "repeating basis, such as every morning or tomorrow at 9. You may "
                "offer it when the work needs a later look, but call it only once "
                "the user wants it."
            ),
            pitfalls=(
                "Every call asks the user to approve the schedule and the prompt, "
                "and nothing is scheduled if they decline. Write the prompt so it "
                "stands on its own when it arrives later: what to do, and what to "
                "compare against or report. A turn runs only while no other turn "
                "of this conversation runs or waits for the user; a missed time "
                "runs once when it can. Three failed turns in a row stop the "
                "schedule. It reaches only this conversation; for other work, ask "
                "the user to start it there."
            ),
        ),
        # Sequential: it pauses the Action for the user's approval.
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="prompt",
                    schema={"type": "string", "minLength": 1, "pattern": r"\S"},
                    required=True,
                    description="The message each scheduled turn sends.",
                ),
                field_spec(
                    name="at",
                    schema=_INSTANT,
                    description=(
                        "One time, ISO 8601 with its offset, such as "
                        "2026-11-22T09:00:00+09:00; not with every."
                    ),
                ),
                field_spec(
                    name="every",
                    schema={
                        "type": ["string", "null"],
                        "pattern": r"^(hour|day|week|month|quarter|[0-9]+[smhd])$",
                    },
                    description=(
                        "hour, day, week, month or quarter, or a duration such as "
                        "30m; not with at."
                    ),
                ),
                field_spec(
                    name="offset",
                    schema={"type": ["string", "null"], "pattern": _DURATION_PATTERN},
                    description=(
                        "With a calendar unit only: one number and unit (s, m, h, "
                        "d) after the unit's start, shorter than the unit."
                    ),
                ),
                field_spec(
                    name="until",
                    schema=_INSTANT,
                    description="When the schedule ends, ISO 8601 with its offset.",
                ),
                field_spec(
                    name="task_id",
                    schema={**_TASK_ID, "type": ["string", "null"]},
                    description=(
                        "Omit to add a schedule; a task_id from "
                        "list_scheduled_continuations to replace that one."
                    ),
                ),
            ),
            description="Schedule this conversation to continue later.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["scheduled"]},
                "task_id": {"type": "string"},
                "next_run_at": {"type": ["string", "null"]},
                "message": {"type": "string"},
            },
            "required": ["status", "task_id", "next_run_at", "message"],
            "additionalProperties": False,
        },
    )
)

LIST_SCHEDULED_CONTINUATIONS_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=LIST_SCHEDULED_CONTINUATIONS_TOOL_ID,
        name="List Scheduled Continuations",
        description="List the schedules this conversation made.",
        guide=ToolGuideSpec(
            what=(
                "Lists each schedule this conversation made with its task_id, its "
                "file text (the trigger, then the prompt), its next run, and how "
                "its latest turn went."
            ),
            when=(
                "Before you replace or stop a schedule, or when the user asks what "
                "is scheduled here."
            ),
            pitfalls=(
                "Schedules the user wrote themselves, and those of other "
                "conversations, are not listed. approved false means the user "
                "edited the file since, and it waits for their approval; "
                "stopped_reason means failures stopped it until the user turns it "
                "back on."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(), description="List this conversation's schedules."
        ),
        output_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "text": {"type": "string"},
                            "next_run_at": {"type": ["string", "null"]},
                            "approved": {"type": "boolean"},
                            "enabled": {"type": "boolean"},
                            "error": {"type": ["string", "null"]},
                            "last_result": {"type": ["string", "null"]},
                            "failure_count": {"type": "integer"},
                            "stopped_reason": {"type": ["string", "null"]},
                        },
                        "required": [
                            "task_id",
                            "text",
                            "next_run_at",
                            "approved",
                            "enabled",
                            "error",
                            "last_result",
                            "failure_count",
                            "stopped_reason",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
    )
)

STOP_SCHEDULED_CONTINUATION_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=STOP_SCHEDULED_CONTINUATION_TOOL_ID,
        name="Stop Scheduled Continuation",
        description="Delete one schedule this conversation made.",
        guide=ToolGuideSpec(
            what="Deletes the schedule, so it sends no more turns.",
            when=(
                "When the user asks to stop it, or the work it repeats is done and "
                "the user agreed to end it."
            ),
            pitfalls=(
                "It cannot be undone; schedule again to restore it, which asks the "
                "user. A turn already sent still runs."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="automation_control",
            default_timeout_ms=5_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="task_id",
                    schema=_TASK_ID,
                    required=True,
                    description="A task_id from list_scheduled_continuations.",
                ),
            ),
            description="Delete one of this conversation's schedules.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["stopped"]},
                "task_id": {"type": "string"},
            },
            "required": ["status", "task_id"],
            "additionalProperties": False,
        },
    )
)

__all__ = [
    "CONVERSATION_TASK_TOOL_IDS",
    "LIST_SCHEDULED_CONTINUATIONS_TOOL",
    "LIST_SCHEDULED_CONTINUATIONS_TOOL_ID",
    "SCHEDULE_CONTINUATION_TOOL",
    "SCHEDULE_CONTINUATION_TOOL_ID",
    "STOP_SCHEDULED_CONTINUATION_TOOL",
    "STOP_SCHEDULED_CONTINUATION_TOOL_ID",
]

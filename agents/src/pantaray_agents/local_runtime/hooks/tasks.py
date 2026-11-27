"""The definition of a task an Action schedules: when it fires, what it sends.

A task is one row of ``user_hooks``; its id is ``task.<stem>``. Its trigger is
kept as JSON in the shape the Action gave it, ``{"at"}`` or ``{"every",
"offset"}``, on local time, and read back through the same checks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pantaray_agents.hooks.definitions import (
    At,
    Every,
    HookDefinition,
    HookDefinitionError,
    RunMessage,
    parse_time_trigger,
)
from pantaray_agents.local_runtime.activity_summary_schedule import iso_z, parse_iso_z
from pantaray_agents.local_runtime.runtime.action_message_models import (
    action_hook_request_text,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS,
)

TASK_HOOK_PREFIX: Final = "task."
# A task's turn belongs to the owner settled on, and waits for a connection
# rather than fail without one.
TASK_GATES: Final = ("owner_settled", "background_can_send")


@dataclass(frozen=True, slots=True)
class TaskSchedule:
    """When a task fires: once ``at``, or ``every`` calendar unit or duration
    (``offset`` after each unit's start, on local time); never after ``until``."""

    at: datetime | None
    every: str | None
    offset: str | None
    until: datetime | None


@dataclass(frozen=True, slots=True)
class Task:
    hook_id: str
    schedule: TaskSchedule
    trigger: At | Every
    message: str

    def definition(self, target: str | None) -> HookDefinition:
        """The hook that sends the message to the conversation ``target``, or
        to a new conversation on each fire when None."""

        return HookDefinition(
            id=self.hook_id,
            trigger=self.trigger,
            requires=TASK_GATES,
            run=RunMessage(target=target, message=self.message),
            until=self.schedule.until,
        )


def build_task(hook_id: str, schedule: TaskSchedule, message: str) -> Task:
    """The task, checked as the engine will read it.

    Raises:
        HookDefinitionError: the schedule or the message cannot fire.
    """

    message = message.strip()
    if not message:
        raise HookDefinitionError("the prompt is empty")
    if len(action_hook_request_text(message)) > ACTION_MESSAGE_CONTENT_MAX_CODEPOINTS:
        raise HookDefinitionError("the prompt is too long for one message")
    table: dict[str, object]
    if schedule.at is not None:
        table = {"at": schedule.at}
    else:
        every = str(schedule.every)
        table = {"every": every, "tz": "local"}
        if schedule.offset is not None:
            table["offset"] = schedule.offset
        # A duration repeats from the approval; a calendar unit has its grid.
        if every[0].isdigit():
            table["align"] = False
    trigger = parse_time_trigger(table, f"hook {hook_id}")
    return Task(hook_id=hook_id, schedule=schedule, trigger=trigger, message=message)


def trigger_json(schedule: TaskSchedule) -> str:
    if schedule.at is not None:
        return json.dumps({"at": iso_z(schedule.at)})
    return json.dumps({"every": schedule.every, "offset": schedule.offset})


def stored_task(
    hook_id: str, stored_trigger: str, until: str | None, message: str
) -> Task:
    """A task as its row keeps it; the row was written by ``build_task``."""

    trigger: dict[str, str | None] = json.loads(stored_trigger)
    at = trigger.get("at")
    schedule = TaskSchedule(
        at=None if at is None else parse_iso_z(at),
        every=trigger.get("every"),
        offset=trigger.get("offset"),
        until=None if until is None else parse_iso_z(until),
    )
    return build_task(hook_id, schedule, message)

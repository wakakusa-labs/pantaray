"""Which hooks may fire for an owner.

A built-in hook always fires. A task is a row of ``user_hooks``; it fires only
while it is enabled and not stopped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pantaray_agents.hooks.definitions import HookDefinition
from pantaray_agents.local_runtime.activity_summary_schedule import parse_iso_z

from .tasks import Task, stored_task

StoppedReason = Literal["start_failed", "turn_failed"]
TurnResult = Literal["completed", "failed", "canceled", "not_started"]


@dataclass(frozen=True, slots=True)
class RunState:
    last_message_id: str | None
    # None while the latest turn has not ended.
    last_result: TurnResult | None
    failure_count: int
    stopped_reason: StoppedReason | None


@dataclass(frozen=True, slots=True)
class NewConversation:
    """Start a new conversation on each fire, as that of a new TODO titled
    ``title`` in the organization or project named (neither: none)."""

    title: str
    organization_id: str | None
    project_id: str | None


@dataclass(frozen=True, slots=True)
class TaskRow:
    task: Task
    # Grows when the definition changes; a fire binds to the one it read.
    version: int
    # The conversation that asked for the task.
    origin_action_id: str
    # The conversation each fire continues, or how each fire starts one.
    target: str | NewConversation
    enabled: bool
    next_fire_at: datetime | None
    run: RunState

    @property
    def hook(self) -> HookDefinition:
        return self.task.definition(
            self.target if isinstance(self.target, str) else None
        )


@dataclass(frozen=True, slots=True)
class ActiveHooks:
    """The hooks that may fire now, and the tasks among them."""

    hooks: tuple[HookDefinition, ...]
    tasks: Mapping[str, TaskRow]


def read_tasks(
    connection: sqlite3.Connection, *, user_id: str, origin: str | None = None
) -> tuple[TaskRow, ...]:
    """The owner's tasks in id order; only those ``origin`` asked for when set."""

    rows = connection.execute(
        """SELECT hook_id, trigger_json, until, message, version, origin_action_id,
                  target_action_id, enabled, next_fire_at, last_message_id,
                  last_result, failure_count, stopped_reason, title,
                  organization_id, project_id
           FROM user_hooks
           WHERE user_id = ? AND (? IS NULL OR origin_action_id = ?)
           ORDER BY hook_id""",
        (user_id, origin, origin),
    ).fetchall()
    return tuple(
        TaskRow(
            task=stored_task(str(row[0]), str(row[1]), row[2], str(row[3])),
            version=int(row[4]),
            origin_action_id=str(row[5]),
            # The table holds a title exactly when it holds no target.
            target=NewConversation(
                title=str(row[13]), organization_id=row[14], project_id=row[15]
            )
            if row[6] is None
            else str(row[6]),
            enabled=bool(row[7]),
            next_fire_at=None if row[8] is None else parse_iso_z(row[8]),
            run=RunState(
                last_message_id=row[9],
                last_result=row[10],
                failure_count=int(row[11]),
                stopped_reason=row[12],
            ),
        )
        for row in rows
    )


def active_hooks(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    builtins: Sequence[HookDefinition],
) -> ActiveHooks:
    tasks = {
        row.task.hook_id: row
        for row in read_tasks(connection, user_id=user_id)
        if row.enabled and row.run.stopped_reason is None
    }
    return ActiveHooks(
        hooks=(*builtins, *(row.hook for row in tasks.values())),
        tasks=tasks,
    )


def set_enabled_in_connection(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str, enabled: bool
) -> bool:
    """Switch a task; enabling a stopped one also clears its failures. False
    when no task has the id."""

    return (
        connection.execute(
            """UPDATE user_hooks SET enabled = :enabled,
                 failure_count = CASE WHEN :enabled THEN 0 ELSE failure_count END,
                 stopped_reason = CASE WHEN :enabled THEN NULL ELSE stopped_reason END
               WHERE user_id = :user_id AND hook_id = :hook_id""",
            {"user_id": user_id, "hook_id": hook_id, "enabled": int(enabled)},
        ).rowcount
        == 1
    )

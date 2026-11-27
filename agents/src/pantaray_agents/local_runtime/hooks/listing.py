"""The owner's view of the tasks Actions scheduled, and the switch they set."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.hooks.definitions import hook_has_ended
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from .controls import (
    StoppedReason,
    TurnResult,
    read_tasks,
    set_enabled_in_connection,
)


@dataclass(frozen=True, slots=True)
class TaskView:
    hook_id: str
    at: datetime | None
    every: str | None
    offset: str | None
    until: datetime | None
    message: str
    enabled: bool
    next_fire_at: datetime | None
    # The conversation that asked for the task.
    created_from_action_id: str
    # The conversation each fire continues; None starts a new one each time.
    target_action_id: str | None
    last_result: TurnResult | None
    failure_count: int
    stopped_reason: StoppedReason | None


def list_hooks(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    origin: str | None = None,
    now: datetime | None = None,
) -> tuple[TaskView, ...]:
    """The owner's tasks; only those ``origin`` asked for when set."""

    now = datetime.now(UTC) if now is None else now
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = read_tasks(connection, user_id=user_id, origin=origin)
    return tuple(
        TaskView(
            hook_id=row.task.hook_id,
            at=row.task.schedule.at,
            every=row.task.schedule.every,
            offset=row.task.schedule.offset,
            until=row.task.schedule.until,
            message=row.task.message,
            enabled=row.enabled,
            next_fire_at=None if hook_has_ended(row.hook, now) else row.next_fire_at,
            created_from_action_id=row.origin_action_id,
            target_action_id=row.target_action_id,
            last_result=row.run.last_result,
            failure_count=row.run.failure_count,
            stopped_reason=row.run.stopped_reason,
        )
        for row in rows
    )


def set_hook_enabled(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, hook_id: str, enabled: bool
) -> bool:
    """Switch a task on or off; False when no task has the id."""

    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            return set_enabled_in_connection(
                connection, user_id=user_id, hook_id=hook_id, enabled=enabled
            )

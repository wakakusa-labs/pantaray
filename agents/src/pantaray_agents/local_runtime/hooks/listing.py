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
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .controls import (
    NO_CONVERSATION,
    StoppedReason,
    TurnResult,
    control_of,
    read_controls,
    read_task_conversations,
    read_task_origins,
    set_enabled_in_connection,
    task_origin_in_connection,
)
from .states import SqliteHookStateStore
from .tasks import InvalidTask, Task, read_task_files


@dataclass(frozen=True, slots=True)
class TaskView:
    hook_id: str
    # The file as it is now.
    text: str
    # False, and it does not fire, once the file is not what the user approved.
    approved: bool
    enabled: bool
    # Why the file cannot fire until it is fixed.
    error: str | None
    next_fire_at: datetime | None
    # The conversation the task was written from, which it continues.
    created_from_action_id: str
    last_result: TurnResult | None
    failure_count: int
    stopped_reason: StoppedReason | None


def list_hooks(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, now: datetime | None = None
) -> tuple[TaskView, ...]:
    now = datetime.now(UTC) if now is None else now
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        controls = read_controls(connection, user_id=user_id)
        conversations = read_task_conversations(connection, user_id=user_id)
        origins = read_task_origins(connection, user_id=user_id)
    fire_times = SqliteHookStateStore(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    ).read_next_fire_times()
    views: list[TaskView] = []
    for task in read_task_files(origins):
        control = control_of(controls, task.hook_id)
        conversation = conversations.get(task.hook_id, NO_CONVERSATION)
        views.append(
            TaskView(
                hook_id=task.hook_id,
                text=task.text,
                approved=control.approved_hash == task.content_hash,
                enabled=control.enabled,
                error=task.error if isinstance(task, InvalidTask) else None,
                next_fire_at=fire_times.get(task.hook_id)
                if isinstance(task, Task) and not hook_has_ended(task.hook, now)
                else None,
                created_from_action_id=origins[task.hook_id],
                last_result=conversation.last_result,
                failure_count=conversation.failure_count,
                stopped_reason=conversation.stopped_reason,
            )
        )
    return tuple(views)


def set_hook_enabled(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, hook_id: str, enabled: bool
) -> bool:
    """Switch a task on or off; False when no task has the id."""

    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            # Read under the write lock a deletion holds, so a task deleted
            # meanwhile gets no switch row.
            if (
                task_origin_in_connection(connection, user_id=user_id, hook_id=hook_id)
                is None
            ):
                return False
            set_enabled_in_connection(
                connection, user_id=user_id, hook_id=hook_id, enabled=enabled
            )
    return True

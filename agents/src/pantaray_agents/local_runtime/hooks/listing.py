"""The owner's view of the tasks Actions scheduled, and the switch they set."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.hooks.definitions import FieldMatch, hook_has_ended
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.todos.action_link import read_todo_for_action
from pantaray_agents.local_runtime.todos.db import read_todo_row

from .controls import (
    StoppedReason,
    TaskRow,
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
    # The event an on task fires on; None for one that fires on time.
    on: str | None
    match: tuple[FieldMatch, ...]
    until: datetime | None
    message: str
    enabled: bool
    next_fire_at: datetime | None
    # The conversation that asked for the task, and its TODO's text.
    created_from_action_id: str
    created_from_title: str | None
    # The conversation each fire continues; None starts a new one each time,
    # as the conversation of a new TODO with the title, in the place named.
    target_action_id: str | None
    title: str | None
    organization_id: str | None
    project_id: str | None
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
        # The TODO rows are read by column name.
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        rows = read_tasks(connection, user_id=user_id, origin=origin)
        titles = {
            action_id: _conversation_title(connection, user_id, action_id)
            for action_id in {row.origin_action_id for row in rows}
        }
    return tuple(_view(row, titles[row.origin_action_id], now) for row in rows)


def _conversation_title(
    connection: sqlite3.Connection, user_id: str, action_id: str
) -> str | None:
    """What the screens call a conversation: its TODO's text.

    Design limit: two reads per conversation that asked, fine for the tens of
    tasks one owner keeps; read them in one query if the list gets slow.
    """

    todo_id = read_todo_for_action(user_id=user_id, action_id=action_id)
    todo = (
        None
        if todo_id is None
        else read_todo_row(connection, user_id=user_id, todo_id=todo_id)
    )
    return None if todo is None else todo.text


def _view(row: TaskRow, origin_title: str | None, now: datetime) -> TaskView:
    target, new = (
        (row.target, None) if isinstance(row.target, str) else (None, row.target)
    )
    return TaskView(
        hook_id=row.task.hook_id,
        at=row.task.schedule.at,
        every=row.task.schedule.every,
        offset=row.task.schedule.offset,
        on=row.task.schedule.on,
        match=row.task.schedule.match,
        until=row.task.schedule.until,
        message=row.task.message,
        enabled=row.enabled,
        next_fire_at=None if hook_has_ended(row.hook, now) else row.next_fire_at,
        created_from_action_id=row.origin_action_id,
        created_from_title=origin_title,
        target_action_id=target,
        title=new.title if new else None,
        organization_id=new.organization_id if new else None,
        project_id=new.project_id if new else None,
        last_result=row.run.last_result,
        failure_count=row.run.failure_count,
        stopped_reason=row.run.stopped_reason,
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

"""The tasks an Action schedules to continue its own conversation.

The Action's task tools are the one writer of ``user_hooks``. A task they save
continues the conversation that saved it, and is saved only once the user said
yes to it in that conversation: one yes saves exactly what the user saw.
Deleting the conversation deletes its tasks through the table's references.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pantaray_agents.local_runtime.activity_summary_schedule import iso_z
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row

from .listing import TaskView, list_hooks
from .states import first_fire_after
from .tasks import TASK_HOOK_PREFIX, Task, trigger_json

TaskChange = Literal["create", "update"]
# not_found: no task of this conversation has the id; exists: a create that
# already ran; ended: its first run would come at or after its until, so it
# would never run.
TaskRefusal = Literal["not_found", "exists", "ended"]
# paused: switched off, or stopped by failures in a row.
TaskState = Literal["on", "paused"]

_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "pantaray:agent_task:v1")
_CHANGED: Final = (
    "(trigger_json, until, message) IS NOT (:trigger_json, :until, :message)"
)


def new_task_id(tool_request_id: str) -> str:
    """The id a create names; one per tool request, so a repeat finds it."""

    stem = uuid.uuid5(_NAMESPACE, tool_request_id).hex[:12]
    return f"{TASK_HOOK_PREFIX}conversation-{stem}"


def _connect(db_path: Path, busy_timeout_ms: int) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    configure_connection(connection, busy_timeout_ms)
    return closing(connection)


def conversation_task_state(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, action_id: str, hook_id: str
) -> TaskState | None:
    """Whether ``hook_id`` is a task this conversation saved and still has, and
    whether it is on, or paused: switched off, or stopped by failures."""

    with _connect(db_path, busy_timeout_ms) as connection:
        row = connection.execute(
            """SELECT enabled = 1 AND stopped_reason IS NULL FROM user_hooks
               WHERE user_id = ? AND hook_id = ? AND origin_action_id = ?""",
            (user_id, hook_id, action_id),
        ).fetchone()
    if row is None:
        return None
    return "on" if row[0] else "paused"


def save_task(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    change: TaskChange,
    task: Task,
    now: datetime,
) -> TaskRefusal | None:
    """Save the task the user said yes to, bound to the conversation
    ``action_id``; an update replaces one of its own tasks.

    The user's yes means they want it to run, so a paused task is switched
    back on, its failures cleared. A changed definition starts its schedule
    afresh at its first occurrence after ``now``, so a spent at moved later
    fires on its new time and an every saved mid-period waits for its next
    occurrence; saving the same definition again keeps the schedule.
    """

    # Computed once: the time checked against until is the time stored.
    first_fire_at = first_fire_after(task.trigger, now)
    until = task.schedule.until
    if first_fire_at is not None and until is not None and first_fire_at >= until:
        return "ended"
    values = {
        "user_id": user_id,
        "hook_id": task.hook_id,
        "action_id": action_id,
        "trigger_json": trigger_json(task.schedule),
        "until": None if until is None else iso_z(until),
        "message": task.message,
        "next_fire_at": None if first_fire_at is None else iso_z(first_fire_at),
    }
    with _connect(db_path, busy_timeout_ms) as connection:
        with immediate_transaction(connection):
            if change == "create":
                ensure_user_row(connection, user_id=user_id)
                inserted = connection.execute(
                    """INSERT INTO user_hooks(user_id, hook_id, origin_action_id,
                         target_action_id, trigger_json, until, message,
                         next_fire_at)
                       VALUES (:user_id, :hook_id, :action_id, :action_id,
                         :trigger_json, :until, :message, :next_fire_at)
                       ON CONFLICT(user_id, hook_id) DO NOTHING""",
                    values,
                )
                return None if inserted.rowcount == 1 else "exists"
            # Every SET reads the row as it was before the update.
            updated = connection.execute(
                f"""UPDATE user_hooks SET
                     version = version + ({_CHANGED}),
                     next_fire_at = CASE WHEN {_CHANGED}
                       THEN :next_fire_at ELSE next_fire_at END,
                     trigger_json = :trigger_json, until = :until,
                     message = :message, enabled = 1, failure_count = 0,
                     stopped_reason = NULL
                   WHERE user_id = :user_id AND hook_id = :hook_id
                     AND origin_action_id = :action_id""",  # noqa: S608
                values,
            )
    return None if updated.rowcount == 1 else "not_found"


def list_conversation_tasks(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, action_id: str
) -> tuple[TaskView, ...]:
    return list_hooks(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        origin=action_id,
    )


def stop_task(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, action_id: str, hook_id: str
) -> TaskRefusal | None:
    """Delete one of the conversation's tasks with its state and inbox."""

    with _connect(db_path, busy_timeout_ms) as connection, connection:
        deleted = connection.execute(
            """DELETE FROM user_hooks
               WHERE user_id = ? AND hook_id = ? AND origin_action_id = ?""",
            (user_id, hook_id, action_id),
        )
    return None if deleted.rowcount == 1 else "not_found"


__all__ = [
    "TaskChange",
    "TaskRefusal",
    "TaskState",
    "conversation_task_state",
    "list_conversation_tasks",
    "new_task_id",
    "save_task",
    "stop_task",
]

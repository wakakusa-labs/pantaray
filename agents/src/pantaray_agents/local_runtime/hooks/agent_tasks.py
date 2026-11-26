"""The tasks an Action schedules to continue its own conversation.

The Action's task tools are the one writer of ~/.pantaray/tasks besides the
user. A task they write continues the conversation that wrote it and nothing
else, and is written only once the user said yes to its content in that
conversation: the write approves exactly that content. Deleting the
conversation deletes its tasks, so none of them opens a new conversation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.tools.files.pantaray_tasks import pantaray_tasks_dir

from .controls import (
    NO_CONVERSATION,
    approve_in_connection,
    control_of,
    read_controls,
    read_task_conversations,
    set_enabled_in_connection,
)
from .listing import TaskView, list_hooks
from .states import first_fire_after
from .tasks import TASK_HOOK_PREFIX, Task, TaskFile, parse_task_file, task_file_path

TaskChange = Literal["create", "update"]
# not_found: no task of this conversation has the id; exists: a create that
# already ran.
TaskRefusal = Literal["not_found", "exists"]
# paused: switched off, or stopped by failures in a row.
TaskState = Literal["on", "paused"]

_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "pantaray:agent_task:v1")
# The rows a task keeps besides its file, each keyed by (user_id, hook_id).
_TASK_TABLES: Final = (
    "hook_settings",
    "hook_states",
    "hook_inbox",
    "hook_conversations",
    "hook_task_origins",
)


@dataclass(frozen=True, slots=True)
class TaskSchedule:
    """When a task fires: once ``at``, or ``every`` calendar unit or duration
    (``offset`` after each unit's start, on local time); never after ``until``."""

    at: datetime | None
    every: str | None
    offset: str | None
    until: datetime | None


def new_task_id(tool_request_id: str) -> str:
    """The id a create names; one per tool request, so a repeat finds it."""

    stem = uuid.uuid5(_NAMESPACE, tool_request_id).hex[:12]
    return f"{TASK_HOOK_PREFIX}conversation-{stem}"


def task_text(schedule: TaskSchedule, prompt: str) -> str:
    """The task file: TOML front matter with the trigger, then the prompt."""

    if schedule.at is not None:
        trigger = f"{{ at = {schedule.at.isoformat()} }}"
    else:
        every = str(schedule.every)
        fields = [f"every = {json.dumps(every)}"]
        if schedule.offset is not None:
            fields.append(f"offset = {json.dumps(schedule.offset)}")
        # A duration repeats from the approval; a calendar unit has its grid.
        if every[0].isdigit():
            fields.append("align = false")
        fields.append('tz = "local"')
        trigger = "{ " + ", ".join(fields) + " }"
    lines = ["+++", 'conversation = "continue"', f"trigger = {trigger}"]
    if schedule.until is not None:
        lines.append(f"until = {schedule.until.isoformat()}")
    return "\n".join([*lines, "+++", prompt.strip(), ""])


def parse_task(hook_id: str, text: str) -> TaskFile:
    return parse_task_file(hook_id.removeprefix(TASK_HOOK_PREFIX), text.encode("utf-8"))


def _connect(db_path: Path, busy_timeout_ms: int) -> closing[sqlite3.Connection]:
    connection = sqlite3.connect(db_path)
    configure_connection(connection, busy_timeout_ms)
    return closing(connection)


def _origin(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str
) -> str | None:
    row = connection.execute(
        "SELECT action_id FROM hook_task_origins WHERE user_id = ? AND hook_id = ?",
        (user_id, hook_id),
    ).fetchone()
    return None if row is None else str(row[0])


def conversation_task_state(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, action_id: str, hook_id: str
) -> TaskState | None:
    """Whether ``hook_id`` is a task this conversation wrote and still has, and
    whether it is on, or paused: switched off, or stopped by failures."""

    with _connect(db_path, busy_timeout_ms) as connection:
        if _origin(connection, user_id=user_id, hook_id=hook_id) != action_id:
            return None
        control = control_of(read_controls(connection, user_id=user_id), hook_id)
        stopped = (
            read_task_conversations(connection, user_id=user_id)
            .get(hook_id, NO_CONVERSATION)
            .stopped_reason
        )
    if not task_file_path(hook_id).is_file():
        return None
    return "on" if control.enabled and stopped is None else "paused"


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
    """Write the task the user said yes to, approved and bound to the
    conversation ``action_id``; an update replaces one of its own tasks.

    The user's yes means they want it to run, so a paused task is switched
    back on, its failures cleared, with the approval.

    The file is written inside the transaction, just before it commits: a
    failed write leaves no rows, and a failed commit leaves a file with no
    approval, which never fires.
    """

    hook_id = task.hook_id
    path = task_file_path(hook_id)
    with _connect(db_path, busy_timeout_ms) as connection:
        with immediate_transaction(connection):
            owner = _origin(connection, user_id=user_id, hook_id=hook_id)
            if change == "create":
                if owner is not None or path.exists():
                    return "exists"
                ensure_user_row(connection, user_id=user_id)
                connection.execute(
                    "INSERT INTO hook_task_origins(user_id, hook_id, action_id) "
                    "VALUES (?, ?, ?)",
                    (user_id, hook_id, action_id),
                )
                # The task continues this conversation from its first fire.
                connection.execute(
                    "INSERT INTO hook_conversations(user_id, hook_id, action_id) "
                    "VALUES (?, ?, ?)",
                    (user_id, hook_id, action_id),
                )
            elif owner != action_id or not path.is_file():
                return "not_found"
            approve_in_connection(
                connection,
                user_id=user_id,
                hook_id=hook_id,
                content_hash=task.content_hash,
                first_fire_at=first_fire_after(task.hook, now),
            )
            set_enabled_in_connection(
                connection, user_id=user_id, hook_id=hook_id, enabled=True
            )
            _write_atomically(path, task.text)
    return None


def list_conversation_tasks(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, action_id: str
) -> tuple[TaskView, ...]:
    return tuple(
        view
        for view in list_hooks(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        )
        if isinstance(view, TaskView) and view.created_from_action_id == action_id
    )


def stop_task(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, action_id: str, hook_id: str
) -> TaskRefusal | None:
    """Delete one of the conversation's tasks: its file and its state."""

    with _connect(db_path, busy_timeout_ms) as connection:
        with immediate_transaction(connection):
            if _origin(connection, user_id=user_id, hook_id=hook_id) != action_id:
                return "not_found"
            _delete_tasks(connection, user_id=user_id, hook_ids=(hook_id,))
    return None


def delete_conversation_tasks_in_connection(
    connection: sqlite3.Connection, *, user_id: str, action_id: str
) -> tuple[str, ...]:
    """Delete the tasks the conversation wrote, files and state, in the
    caller's transaction."""

    hook_ids = tuple(
        str(hook_id)
        for (hook_id,) in connection.execute(
            "SELECT hook_id FROM hook_task_origins WHERE user_id = ? AND action_id = ?",
            (user_id, action_id),
        )
    )
    _delete_tasks(connection, user_id=user_id, hook_ids=hook_ids)
    return hook_ids


def _delete_tasks(
    connection: sqlite3.Connection, *, user_id: str, hook_ids: tuple[str, ...]
) -> None:
    """Delete the tasks' state, then their files, before the transaction
    commits: an approval reads the file under the same write lock, so it
    never sees a file whose task is gone. A failed removal rolls the deletion
    back; a failed commit leaves state with no file, which never fires."""

    for table in _TASK_TABLES:
        connection.executemany(
            f"DELETE FROM {table} WHERE user_id = ? AND hook_id = ?",  # noqa: S608
            [(user_id, hook_id) for hook_id in hook_ids],
        )
    for hook_id in hook_ids:
        with suppress(FileNotFoundError):
            task_file_path(hook_id).unlink()


def _write_atomically(path: Path, text: str) -> None:
    folder = pantaray_tasks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        # A file ending in .md only when whole: the tasks folder is read as
        # it is written.
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


__all__ = [
    "TaskChange",
    "TaskRefusal",
    "TaskSchedule",
    "delete_conversation_tasks_in_connection",
    "conversation_task_state",
    "TaskState",
    "list_conversation_tasks",
    "new_task_id",
    "parse_task",
    "save_task",
    "stop_task",
    "task_text",
]

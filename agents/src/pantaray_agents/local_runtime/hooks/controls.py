"""Which hooks may fire for an owner: the owner's switches and approvals.

A built-in hook fires unless the owner disabled it. A task fires only while it
is enabled, not stopped, and its file still hashes to the approved content.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.hooks.definitions import HookDefinition
from pantaray_agents.local_runtime.storage.users import ensure_user_row

from .tasks import Task, TaskFile

StoppedReason = Literal["start_failed", "turn_failed"]
TurnResult = Literal["completed", "failed", "canceled", "not_started"]


@dataclass(frozen=True, slots=True)
class HookControl:
    enabled: bool
    approved_hash: str | None


@dataclass(frozen=True, slots=True)
class TaskConversation:
    action_id: str | None
    last_message_id: str | None
    # None while the latest turn has not ended.
    last_result: TurnResult | None
    failure_count: int
    stopped_reason: StoppedReason | None


NO_CONVERSATION = TaskConversation(
    action_id=None,
    last_message_id=None,
    last_result=None,
    failure_count=0,
    stopped_reason=None,
)
_DEFAULT_CONTROL = HookControl(enabled=True, approved_hash=None)


@dataclass(frozen=True, slots=True)
class ActiveHooks:
    """The hooks that may fire now, and the approved tasks among them."""

    hooks: tuple[HookDefinition, ...]
    tasks: Mapping[str, Task]
    conversations: Mapping[str, TaskConversation]


def read_controls(
    connection: sqlite3.Connection, *, user_id: str
) -> Mapping[str, HookControl]:
    return {
        str(hook_id): HookControl(
            enabled=bool(enabled),
            approved_hash=None if approved_hash is None else str(approved_hash),
        )
        for hook_id, enabled, approved_hash in connection.execute(
            "SELECT hook_id, enabled, approved_hash FROM hook_settings "
            "WHERE user_id = ?",
            (user_id,),
        )
    }


def read_task_conversations(
    connection: sqlite3.Connection, *, user_id: str
) -> Mapping[str, TaskConversation]:
    rows = connection.execute(
        """SELECT hook_id, action_id, last_message_id, last_result, failure_count,
                  stopped_reason
           FROM hook_conversations WHERE user_id = ?""",
        (user_id,),
    ).fetchall()
    return {
        str(hook_id): TaskConversation(
            action_id=action_id,
            last_message_id=last_message_id,
            last_result=last_result,
            failure_count=int(failure_count),
            stopped_reason=stopped_reason,
        )
        for (
            hook_id,
            action_id,
            last_message_id,
            last_result,
            failure_count,
            stopped_reason,
        ) in rows
    }


def control_of(controls: Mapping[str, HookControl], hook_id: str) -> HookControl:
    return controls.get(hook_id, _DEFAULT_CONTROL)


def active_hooks(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    builtins: Sequence[HookDefinition],
    task_files: Sequence[TaskFile],
) -> ActiveHooks:
    controls = read_controls(connection, user_id=user_id)
    conversations = read_task_conversations(connection, user_id=user_id)
    tasks = {
        task.hook.id: task
        for task in task_files
        if isinstance(task, Task)
        and control_of(controls, task.hook.id).enabled
        and control_of(controls, task.hook.id).approved_hash == task.content_hash
        and conversations.get(task.hook.id, NO_CONVERSATION).stopped_reason is None
    }
    return ActiveHooks(
        hooks=(
            *(hook for hook in builtins if control_of(controls, hook.id).enabled),
            *(task.hook for task in tasks.values()),
        ),
        tasks=tasks,
        conversations=conversations,
    )


def set_enabled_in_connection(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str, enabled: bool
) -> None:
    """Switch a hook; enabling a stopped task also clears its failures."""

    ensure_user_row(connection, user_id=user_id)
    connection.execute(
        """INSERT INTO hook_settings(user_id, hook_id, enabled) VALUES (?, ?, ?)
           ON CONFLICT(user_id, hook_id) DO UPDATE SET enabled = excluded.enabled""",
        (user_id, hook_id, int(enabled)),
    )
    if enabled:
        connection.execute(
            """UPDATE hook_conversations SET failure_count = 0, stopped_reason = NULL
               WHERE user_id = ? AND hook_id = ?""",
            (user_id, hook_id),
        )


def approve_in_connection(
    connection: sqlite3.Connection, *, user_id: str, hook_id: str, content_hash: str
) -> None:
    ensure_user_row(connection, user_id=user_id)
    connection.execute(
        """INSERT INTO hook_settings(user_id, hook_id, enabled, approved_hash)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(user_id, hook_id)
           DO UPDATE SET approved_hash = excluded.approved_hash""",
        (user_id, hook_id, content_hash),
    )

"""The owner's view of every hook, and the switches and approvals they set."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Literal

from pantaray_agents.hooks.definitions import (
    At,
    Every,
    HookDefinition,
    hook_has_ended,
)
from pantaray_agents.hooks.schedule import latest_occurrence
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from . import runner
from .builtin import builtin_hooks
from .controls import (
    NO_CONVERSATION,
    StoppedReason,
    TurnResult,
    approve_in_connection,
    control_of,
    read_controls,
    read_task_conversations,
    set_enabled_in_connection,
)
from .tasks import InvalidTask, Task, read_task_files

HookChangeRefusal = Literal["not_found", "not_a_task", "invalid", "changed"]


@dataclass(frozen=True, slots=True)
class BuiltinHookView:
    hook_id: str
    enabled: bool
    next_fire_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskView:
    hook_id: str
    # The file as it is now; approving it approves exactly this content.
    text: str
    content_hash: str
    approved: bool
    enabled: bool
    # Why the file cannot fire until it is fixed.
    error: str | None
    next_fire_at: datetime | None
    action_id: str | None
    last_result: TurnResult | None
    failure_count: int
    stopped_reason: StoppedReason | None


HookView = BuiltinHookView | TaskView


def list_hooks(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, now: datetime | None = None
) -> tuple[HookView, ...]:
    now = datetime.now(UTC) if now is None else now
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        controls = read_controls(connection, user_id=user_id)
        conversations = read_task_conversations(connection, user_id=user_id)
    fire_times = runner.SqliteHookStateStore(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    ).read_next_fire_times()

    def next_fire_at(hook: HookDefinition) -> datetime | None:
        if not isinstance(hook.trigger, At | Every) or hook_has_ended(hook, now):
            return None
        if hook.id in fire_times:
            return fire_times[hook.id]
        zone: tzinfo = (
            runner.local_zone()
            if isinstance(hook.trigger, Every) and hook.trigger.tz == "local"
            else UTC
        )
        return latest_occurrence(hook.trigger, now, zone)

    views: list[HookView] = [
        BuiltinHookView(
            hook_id=hook.id,
            enabled=control_of(controls, hook.id).enabled,
            next_fire_at=next_fire_at(hook),
        )
        for hook in builtin_hooks()
    ]
    for task in read_task_files():
        control = control_of(controls, task.hook_id)
        conversation = conversations.get(task.hook_id, NO_CONVERSATION)
        views.append(
            TaskView(
                hook_id=task.hook_id,
                text=task.text,
                content_hash=task.content_hash,
                approved=control.approved_hash == task.content_hash,
                enabled=control.enabled,
                error=task.error if isinstance(task, InvalidTask) else None,
                next_fire_at=next_fire_at(task.hook)
                if isinstance(task, Task)
                else None,
                action_id=conversation.action_id,
                last_result=conversation.last_result,
                failure_count=conversation.failure_count,
                stopped_reason=conversation.stopped_reason,
            )
        )
    return tuple(views)


def approve_task(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    hook_id: str,
    content_hash: str,
) -> HookChangeRefusal | None:
    """Approve the task's content the owner saw, named by its hash."""

    if any(hook.id == hook_id for hook in builtin_hooks()):
        return "not_a_task"
    task = next((t for t in read_task_files() if t.hook_id == hook_id), None)
    if task is None:
        return "not_found"
    if task.content_hash != content_hash:
        return "changed"
    if isinstance(task, InvalidTask):
        return "invalid"
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            approve_in_connection(
                connection, user_id=user_id, hook_id=hook_id, content_hash=content_hash
            )
    return None


def set_hook_enabled(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, hook_id: str, enabled: bool
) -> HookChangeRefusal | None:
    if not any(hook.id == hook_id for hook in builtin_hooks()) and not any(
        task.hook_id == hook_id for task in read_task_files()
    ):
        return "not_found"
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            set_enabled_in_connection(
                connection, user_id=user_id, hook_id=hook_id, enabled=enabled
            )
    return None

"""Run the built-in hooks and the owner's tasks against the local runtime database."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.hooks.definitions import HookDefinition, RunJob
from pantaray_agents.hooks.engine import Fire, FireOutcome, run_hook_tick
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from .builtin import builtin_hooks
from .controls import active_hooks
from .conversations import fire_task, record_turn_results
from .gates import build_gates, build_predicates
from .inbox import SqliteHookInbox
from .jobs import enqueue_hook_job, run_is_active
from .states import SqliteHookStateStore, local_zone


def run_hooks_once(
    *, db_path: Path, busy_timeout_ms: int, user_id: str, now: datetime | None = None
) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        record_turn_results(connection, user_id=user_id)
        active = active_hooks(connection, user_id=user_id, builtins=builtin_hooks())
    gates = build_gates(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    predicates = build_predicates(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )

    def is_active(hook: HookDefinition) -> bool:
        # A task's conversation is checked when its turn is sent.
        return isinstance(hook.run, RunJob) and run_is_active(
            hook.run, db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        )

    def enqueue(fire: Fire) -> FireOutcome:
        task = active.tasks.get(fire.hook.id)
        if task is None:
            return enqueue_hook_job(
                fire, db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
            )
        return fire_task(
            fire,
            task,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
        )

    run_hook_tick(
        active.hooks,
        now=datetime.now(UTC) if now is None else now,
        local_zone=local_zone,
        states=SqliteHookStateStore(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            task_hashes={
                hook_id: task.content_hash for hook_id, task in active.tasks.items()
            },
        ),
        inbox=SqliteHookInbox(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ),
        gate_is_open=lambda name: gates[name](),
        predicate_holds=lambda name, event: predicates[name](event),
        run_is_active=is_active,
        enqueue=enqueue,
    )

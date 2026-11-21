"""The jobs a hook can run, and the enqueue the engine calls when one fires."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from pantaray_agents.hooks.definitions import RunJob
from pantaray_agents.hooks.engine import Fire, FireOutcome
from pantaray_agents.local_runtime.runtime.activity_summary_scheduler import (
    enqueue_scheduled_activity_summary,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    enqueue_short_insight_job,
)
from pantaray_agents.local_runtime.runtime.job_status import (
    ACTIVE_DEDUPE_JOB_STATUSES,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    LOCAL_INSIGHT_JOB_TYPE,
    LOCAL_MEMORY_UPDATE_JOB_TYPE,
    LOCAL_SUGGESTION_JOB_TYPE,
)
from pantaray_agents.local_runtime.runtime.memory_update_queue import (
    enqueue_memory_update_from_inbox,
)
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    enqueue_suggestion_from_inbox,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.todos.pickup import pick_up_todos

from .builtin import TODO_PICKUP_RUN, summary_type
from .inbox import SqliteHookInbox


def run_is_active(
    run: RunJob, *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> bool:
    """Whether a job of the run's type is queued or running for the user."""
    placeholders = ",".join("?" for _ in ACTIVE_DEDUPE_JOB_STATUSES)
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            f"""SELECT 1 FROM jobs
                WHERE user_id = ? AND job_type = ? AND status IN ({placeholders})
                LIMIT 1""",
            (user_id, run.job, *ACTIVE_DEDUPE_JOB_STATUSES),
        ).fetchone()
    return row is not None


def enqueue_hook_job(
    fire: Fire, *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> FireOutcome:
    run = fire.hook.run
    # Only a built-in runs a job: a task's conversation hook goes to fire_task.
    assert isinstance(run, RunJob)
    if run.job == LOCAL_INSIGHT_JOB_TYPE:
        # A later window while a run is active is dropped, not queued: the run
        # reads from the source cursor, so the next window covers it.
        enqueue_short_insight_job(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            moment=fire.occurrence,
        )
        return "done"
    if run.job == LOCAL_MEMORY_UPDATE_JOB_TYPE:
        enqueue_memory_update_from_inbox(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            hook_id=fire.hook.id,
            events=fire.events,
            superseded=fire.superseded,
            enqueued_at=format_utc_iso(fire.now),
        )
        return "done"
    if run.job == LOCAL_SUGGESTION_JOB_TYPE:
        (request,) = fire.events  # collapse = latest hands the newest alone.
        enqueue_suggestion_from_inbox(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            hook_id=fire.hook.id,
            request=request,
            superseded=fire.superseded,
            enqueued_at=format_utc_iso(fire.now),
        )
        return "done"
    if run.job == TODO_PICKUP_RUN:
        # A pass starts every TODO that is ready, so the events say only that
        # one may be; one saved during the pass stays for the next.
        if not pick_up_todos(user_id=user_id):
            return "retry"
        SqliteHookInbox(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        ).drop(
            fire.hook.id,
            [*(event.delivery_id for event in fire.events), *fire.superseded],
        )
        return "done"
    return asyncio.run(
        enqueue_scheduled_activity_summary(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            summary_type=summary_type(run),
            now=fire.now,
        )
    )

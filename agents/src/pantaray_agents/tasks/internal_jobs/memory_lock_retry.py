from __future__ import annotations

from datetime import timedelta
from typing import Final, NoReturn

from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.job_control import (
    LocalJobDeferEvent,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso, utc_now
from pantaray_agents.tasks.job_retry import defer_local_job_transition_with_retry

MEMORY_UPDATE_LOCK_CONFLICT_EXCEPTION: Final[str] = "MemoryUpdateLockConflictError"
MEMORY_LOCK_RETRY_DELAY_SECONDS: Final[int] = 15


async def defer_memory_update_lock_conflict(
    *,
    job_id: str,
    process_id: str,
    job_type: str,
    process_pending_status: str,
) -> NoReturn:
    now = utc_now()
    scheduled_at = format_utc_iso(
        now + timedelta(seconds=MEMORY_LOCK_RETRY_DELAY_SECONDS)
    )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    await defer_local_job_transition_with_retry(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        job_id=job_id,
        process_id=process_id,
        scheduled_at=scheduled_at,
        process_pending_status=process_pending_status,
        process_event=LocalJobDeferEvent(
            event_name="memory_update_lock_conflict_deferred",
            payload={
                "job_id": job_id,
                "job_type": job_type,
                "scheduled_at": scheduled_at,
                "retry_delay_seconds": MEMORY_LOCK_RETRY_DELAY_SECONDS,
                "exception_type": MEMORY_UPDATE_LOCK_CONFLICT_EXCEPTION,
            },
            created_at=format_utc_iso(now),
        ),
    )

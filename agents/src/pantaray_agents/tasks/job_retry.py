"""Explicit retry transition for local jobs blocked by transient dependencies."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import NoReturn

from pantaray_agents.local_runtime.runtime.job_control import (
    LOCAL_JOB_OPERATIONAL_RETRY_DELAY_SECONDS,
    DeferredLocalJob,
    LocalJobDeferEvent,
    defer_local_job,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso, utc_now
from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)

_LOCAL_JOB_DEFER_RETRY_INITIAL_DELAY_SECONDS = 0.1
_LOCAL_JOB_DEFER_RETRY_MAX_DELAY_SECONDS = 2.0


async def defer_local_job_if_retryable(
    *,
    exc: BaseException,
    job_id: str,
    process_id: str,
    process_pending_status: str,
    db_path: Path | str,
    busy_timeout_ms: int,
    process_event: LocalJobDeferEvent | None = None,
    attempt_error_code: str | None = None,
) -> None:
    if not is_retryable_repository_exception(exc):
        return
    scheduled_at = format_utc_iso(
        utc_now() + timedelta(seconds=LOCAL_JOB_OPERATIONAL_RETRY_DELAY_SECONDS)
    )
    await defer_local_job_transition_with_retry(
        job_id=job_id,
        process_id=process_id,
        process_pending_status=process_pending_status,
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        scheduled_at=scheduled_at,
        process_event=process_event,
        attempt_error_code=attempt_error_code,
    )


async def defer_local_job_transition_with_retry(
    *,
    job_id: str,
    process_id: str,
    process_pending_status: str,
    db_path: Path | str,
    busy_timeout_ms: int,
    scheduled_at: str,
    process_event: LocalJobDeferEvent | None = None,
    attempt_error_code: str | None = None,
) -> NoReturn:
    delay_seconds = _LOCAL_JOB_DEFER_RETRY_INITIAL_DELAY_SECONDS
    while True:
        try:
            defer_local_job(
                db_path=str(db_path),
                busy_timeout_ms=busy_timeout_ms,
                job_id=job_id,
                process_id=process_id,
                scheduled_at=scheduled_at,
                process_pending_status=process_pending_status,
                process_event=process_event,
                attempt_error_code=attempt_error_code,
            )
        except Exception as transition_exc:
            if not is_retryable_repository_exception(transition_exc):
                raise
            await asyncio.sleep(delay_seconds)
            delay_seconds = min(
                delay_seconds * 2,
                _LOCAL_JOB_DEFER_RETRY_MAX_DELAY_SECONDS,
            )
        else:
            raise DeferredLocalJob(job_id=job_id, scheduled_at=scheduled_at)


__all__ = [
    "defer_local_job_if_retryable",
    "defer_local_job_transition_with_retry",
    "LOCAL_JOB_OPERATIONAL_RETRY_DELAY_SECONDS",
]

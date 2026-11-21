from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pantaray_agents.hooks.engine import FireOutcome
from pantaray_agents.local_runtime.activity_summary_schedule import (
    ACTIVITY_SUMMARY_SCHEDULE_SPECS,
    ActivitySummaryScheduleSpec,
    ActivitySummaryWindow,
    SummaryType,
    iso_z,
    latest_eligible_window,
    next_period_end,
    parse_iso_z,
    period_end_floor,
    previous_period_start,
    shorter_summary_types,
    window_from_start,
)
from pantaray_agents.local_runtime.runtime.activity_queue import (
    has_unfinished_activity_summary_job,
)
from pantaray_agents.local_runtime.runtime.activity_summary_execution import (
    ACTIVITY_SUMMARY_MAX_WINDOW_LAG,
    is_activity_summary_window_expired,
)
from pantaray_agents.local_runtime.runtime.identity import current_owner_id
from pantaray_agents.local_runtime.runtime.insight_queue import (
    has_unfinished_short_insight_run,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.orchestration.runtime.activity_job_queue import (
    enqueue_activity_summary_job,
)
from pantaray_agents.utils.activity_ids import (
    compute_activity_summary_id,
    normalize_iso_to_iso_z,
)

logger = logging.getLogger(__name__)

_SCHEDULER_JOB_STATUS_ACTIVE = "active"
_SCHEDULER_RUN_STATUS_RUNNING = "running"
_SCHEDULER_RUN_STATUS_COMPLETED = "completed"
_SCHEDULER_RUN_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class SchedulerRunClaim:
    run_id: str
    job_name: str
    window: ActivitySummaryWindow


def _utc_now() -> datetime:
    return datetime.now(UTC)


def calculate_summary_window(
    *,
    summary_type: SummaryType,
    now: datetime | None = None,
) -> ActivitySummaryWindow:
    current = _utc_now() if now is None else now.astimezone(UTC)
    period_end = period_end_floor(current, summary_type)
    period_start = previous_period_start(period_end, summary_type)
    return ActivitySummaryWindow(period_start=period_start, period_end=period_end)


def _window_is_expired(window: ActivitySummaryWindow, *, now: datetime) -> bool:
    return is_activity_summary_window_expired(
        period_start=window.period_start,
        period_end=window.period_end,
        now=now,
    )


def _oldest_summarizable_window(
    *,
    spec: ActivitySummaryScheduleSpec,
    now: datetime,
) -> ActivitySummaryWindow:
    """The window the lag limit falls inside; every window before it is expired."""
    return window_from_start(
        period_start=period_end_floor(
            now - ACTIVITY_SUMMARY_MAX_WINDOW_LAG,
            spec.summary_type,
        ),
        summary_type=spec.summary_type,
    )


def _select_candidate_window(
    *,
    spec: ActivitySummaryScheduleSpec,
    now: datetime,
    cursor_value: str | None,
) -> ActivitySummaryWindow | None:
    eligible_window = latest_eligible_window(spec=spec, now=now)
    candidate_window = eligible_window
    if cursor_value is not None:
        candidate_window = window_from_start(
            period_start=next_period_end(parse_iso_z(cursor_value), spec.summary_type),
            summary_type=spec.summary_type,
        )
        if candidate_window.period_start > eligible_window.period_start:
            return None
        if _window_is_expired(candidate_window, now=now):
            # Drop the whole expired backlog in this one tick. Walking it one
            # window per tick would withhold the windows that can still be
            # summarized for hours, and none of the skipped ones is ever
            # summarized. When the limit falls inside the period in progress,
            # only the latest complete period is left to take.
            candidate_window = _oldest_summarizable_window(spec=spec, now=now)
            if candidate_window.period_start > eligible_window.period_start:
                candidate_window = eligible_window
    # The eligible window itself can be expired: inside the grace period after a
    # boundary it is still the period before the one that just ended, which for
    # every type of 24h or longer is already a full period behind.
    if _window_is_expired(candidate_window, now=now):
        return None
    return candidate_window


async def run_summary_generation(
    summary_type: SummaryType,
    *,
    user_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    window = calculate_summary_window(summary_type=summary_type, now=now)
    resolved_user_id = current_owner_id() if user_id is None else user_id

    period_start_z = normalize_iso_to_iso_z(iso_z(window.period_start))
    period_end_z = normalize_iso_to_iso_z(iso_z(window.period_end))
    enqueued_count = 0
    failed_count = 0

    summary_id = compute_activity_summary_id(
        user_id=resolved_user_id,
        summary_type=str(summary_type),
        period_start=period_start_z,
    )
    try:
        enqueue_activity_summary_job(
            user_id=resolved_user_id,
            summary_id=str(summary_id),
            summary_type=str(summary_type),
            period_start=str(period_start_z),
            period_end=str(period_end_z),
        )
        enqueued_count += 1
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Error enqueueing %s summary for user %s: %s",
            summary_type,
            resolved_user_id,
            exc,
            exc_info=True,
        )
        failed_count += 1

    return {
        "total": 1,
        "enqueued": enqueued_count,
        "failed": failed_count,
    }


def _ensure_scheduler_job(
    connection: sqlite3.Connection,
    *,
    spec: ActivitySummaryScheduleSpec,
) -> str:
    connection.execute(
        """
        INSERT INTO scheduler_jobs(
            job_name,
            job_kind,
            status,
            cursor_value,
            last_started_at,
            last_completed_at,
            last_success_at,
            consecutive_failures
        ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, 0)
        ON CONFLICT(job_name) DO NOTHING
        """,
        (spec.job_name, "activity_summary", _SCHEDULER_JOB_STATUS_ACTIVE),
    )
    row = connection.execute(
        """
        SELECT status
        FROM scheduler_jobs
        WHERE job_name = ?
        """,
        (spec.job_name,),
    ).fetchone()
    if row is None:
        raise MigrationError(f"scheduler_job missing after upsert: {spec.job_name}")
    return str(row[0])


def _claim_due_scheduler_run(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    user_id: str,
    spec: ActivitySummaryScheduleSpec,
    now: datetime,
) -> SchedulerRunClaim | Literal["waiting"] | None:
    """Claim the window after the cursor, "waiting" while its sources are open."""
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")

    now_iso = iso_z(now)

    with sqlite3.connect(str(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            status = _ensure_scheduler_job(connection, spec=spec)
            if status != _SCHEDULER_JOB_STATUS_ACTIVE:
                return None

            cursor_row = connection.execute(
                """
                SELECT cursor_value
                FROM scheduler_jobs
                WHERE job_name = ?
                """,
                (spec.job_name,),
            ).fetchone()
            if cursor_row is None:
                raise MigrationError(f"scheduler_job cursor missing: {spec.job_name}")
            cursor_value = str(cursor_row[0]) if cursor_row[0] is not None else None

            window = _select_candidate_window(
                spec=spec,
                now=now,
                cursor_value=cursor_value,
            )
            if window is None:
                return None
            window_start_iso = iso_z(window.period_start)
            window_end_iso = iso_z(window.period_end)
            # Leave the cursor where it is: the hook stays due and re-evaluates
            # this window once the work that owns its source rows has terminated.
            # Every type waits on the short Insight runs of its window, not only
            # the 1h one: a shorter summary's job is itself withheld while a run
            # is open, so an absent shorter job does not yet mean the period is
            # accounted for.
            if has_unfinished_short_insight_run(
                connection,
                user_id=user_id,
                window_start=window_start_iso,
                window_end=window_end_iso,
            ) or has_unfinished_activity_summary_job(
                connection,
                user_id=user_id,
                summary_types=shorter_summary_types(spec.summary_type),
                window_start=window_start_iso,
                window_end=window_end_iso,
            ):
                return "waiting"

            existing_row = connection.execute(
                """
                SELECT run_id, status, attempt
                FROM scheduler_job_runs
                WHERE job_name = ? AND logical_window_start = ?
                """,
                (spec.job_name, window_start_iso),
            ).fetchone()

            if existing_row is None:
                run_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO scheduler_job_runs(
                        run_id,
                        job_name,
                        logical_window_start,
                        logical_window_end,
                        status,
                        attempt,
                        enqueued_at,
                        started_at,
                        completed_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, NULL)
                    """,
                    (
                        run_id,
                        spec.job_name,
                        window_start_iso,
                        window_end_iso,
                        _SCHEDULER_RUN_STATUS_RUNNING,
                        now_iso,
                        now_iso,
                    ),
                )
            else:
                run_id = str(existing_row[0])
                status_value = str(existing_row[1])
                attempt = int(existing_row[2])
                if status_value in {
                    _SCHEDULER_RUN_STATUS_RUNNING,
                    _SCHEDULER_RUN_STATUS_COMPLETED,
                }:
                    return None
                if status_value != _SCHEDULER_RUN_STATUS_FAILED:
                    return None
                connection.execute(
                    """
                    UPDATE scheduler_job_runs
                    SET
                        status = ?,
                        attempt = ?,
                        error_code = NULL,
                        error_details_json = NULL,
                        enqueued_at = ?,
                        started_at = ?,
                        completed_at = NULL
                    WHERE run_id = ?
                    """,
                    (
                        _SCHEDULER_RUN_STATUS_RUNNING,
                        attempt + 1,
                        now_iso,
                        now_iso,
                        run_id,
                    ),
                )

            connection.execute(
                """
                UPDATE scheduler_jobs
                SET last_started_at = ?
                WHERE job_name = ?
                """,
                (now_iso, spec.job_name),
            )
    return SchedulerRunClaim(run_id=run_id, job_name=spec.job_name, window=window)


def _complete_scheduler_run(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    claim: SchedulerRunClaim,
) -> None:
    completed_at = iso_z(_utc_now())
    with sqlite3.connect(str(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            connection.execute(
                """
                UPDATE scheduler_job_runs
                SET status = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (_SCHEDULER_RUN_STATUS_COMPLETED, completed_at, claim.run_id),
            )
            connection.execute(
                """
                UPDATE scheduler_jobs
                SET
                    cursor_value = ?,
                    last_completed_at = ?,
                    last_success_at = ?,
                    consecutive_failures = 0
                WHERE job_name = ?
                """,
                (
                    iso_z(claim.window.period_start),
                    completed_at,
                    completed_at,
                    claim.job_name,
                ),
            )


def _fail_scheduler_run(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    claim: SchedulerRunClaim,
    error_code: str,
    error_message: str,
) -> None:
    completed_at = iso_z(_utc_now())
    error_details_json = json.dumps({"message": error_message}, ensure_ascii=False)
    with sqlite3.connect(str(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        with connection:
            connection.execute(
                """
                UPDATE scheduler_job_runs
                SET
                    status = ?,
                    error_code = ?,
                    error_details_json = ?,
                    completed_at = ?
                WHERE run_id = ?
                """,
                (
                    _SCHEDULER_RUN_STATUS_FAILED,
                    error_code,
                    error_details_json,
                    completed_at,
                    claim.run_id,
                ),
            )
            connection.execute(
                """
                UPDATE scheduler_jobs
                SET
                    last_completed_at = ?,
                    consecutive_failures = consecutive_failures + 1
                WHERE job_name = ?
                """,
                (completed_at, claim.job_name),
            )


async def enqueue_scheduled_activity_summary(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    summary_type: SummaryType,
    now: datetime | None = None,
) -> FireOutcome:
    """Enqueue the next window of ``summary_type`` its hook fired for.

    "retry" keeps the hook due: the window waits for its sources, the enqueue
    failed, or a backlog remains, which then drains one window per tick.
    """
    current = _utc_now() if now is None else now.astimezone(UTC)
    owner_user_id = current_owner_id()
    spec = next(
        spec
        for spec in ACTIVITY_SUMMARY_SCHEDULE_SPECS
        if spec.summary_type == summary_type
    )
    claim = _claim_due_scheduler_run(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=owner_user_id,
        spec=spec,
        now=current,
    )
    if claim is None:
        return "done"
    if claim == "waiting":
        return "retry"
    try:
        result = await run_summary_generation(
            spec.summary_type,
            user_id=owner_user_id,
            now=claim.window.period_end,
        )
        if result["failed"] > 0:
            raise RuntimeError(
                "activity summary scheduler enqueue failed for one or more users"
            )
        logger.info(
            "Activity summary scheduler completed: job_name=%s total=%d enqueued=%d failed=%d",
            spec.job_name,
            result["total"],
            result["enqueued"],
            result["failed"],
        )
        _complete_scheduler_run(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            claim=claim,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Activity summary scheduler failed: job_name=%s error=%s",
            spec.job_name,
            exc,
        )
        _fail_scheduler_run(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            claim=claim,
            error_code="ACTIVITY_SUMMARY_SCHEDULER_FAILED",
            error_message=str(exc),
        )
        return "retry"
    latest = latest_eligible_window(spec=spec, now=current)
    return "retry" if claim.window.period_start < latest.period_start else "done"


__all__ = [
    "ACTIVITY_SUMMARY_SCHEDULE_SPECS",
    "ActivitySummaryScheduleSpec",
    "ActivitySummaryWindow",
    "SummaryType",
    "calculate_summary_window",
    "enqueue_scheduled_activity_summary",
    "run_summary_generation",
]

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from typing import Literal, TypedDict

from pantaray_agents.local_runtime.hooks.events import JobLifecycleEvent, JobPhase
from pantaray_agents.local_runtime.hooks.inbox import record_event
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from .job_envelope import (
    LocalJobEnvelope,
    LocalJobEnvelopeIntegrityError,
    load_local_job_envelope,
    require_valid_local_job_envelope,
)
from .job_status import FINALIZABLE_JOB_STATUSES, JOB_STATUS_QUEUED, JOB_STATUS_RUNNING
from .job_types import (
    ACTION_PROCESS_KIND,
    ACTION_SUBAGENT_PROCESS_KIND,
    LOCAL_ACTION_JOB_TYPE,
    LOCAL_ACTION_SUBAGENT_JOB_TYPE,
)
from .utc_timestamps import now_utc_iso

FINAL_JOB_STATUS = Literal["completed", "failed", "abandoned", "canceled", "blocked"]
PROCESS_RUNTIME_STATUS = Literal[
    "enqueued",
    "running",
    "paused",
    "completed",
    "failed",
    "canceled",
    "success",
    "error",
    "abandoned",
]
PROCESS_TERMINAL_STATUSES: tuple[PROCESS_RUNTIME_STATUS, ...] = (
    "completed",
    "failed",
    "canceled",
    "success",
    "error",
    "abandoned",
)
LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR_CODE = "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR"
# The exception that ended the run carries no code of its own, so the code says
# where the job died and the recorded reason says which exceptions it died of.
LOCAL_JOB_EXECUTION_FAILED_ERROR_CODE = "LOCAL_JOB_EXECUTION_FAILED"
# A subagent child only executes against its exact parent runtime, so it becomes
# claimable after that parent process is running on its own unfenced job. Every
# other job type is admitted unchanged, and so is a job whose child ownership is
# already broken: hiding it here would keep it queued instead of letting the
# envelope check block it.
_CLAIMABLE_PARENT_SQL = f"""
AND (j.job_type <> '{LOCAL_ACTION_SUBAGENT_JOB_TYPE}' OR NOT EXISTS (
  SELECT 1 FROM processes AS child
  JOIN job_payloads AS payload ON payload.job_id = j.job_id
  WHERE child.process_id = j.process_id
    AND child.kind = '{ACTION_SUBAGENT_PROCESS_KIND}'
    AND child.user_id = j.user_id
) OR EXISTS (
  SELECT 1 FROM processes AS child
  JOIN processes AS parent ON parent.process_id = child.parent_process_id
    AND parent.user_id = child.user_id AND parent.action_id = child.action_id
  JOIN jobs AS parent_job ON parent_job.job_id = parent.current_job_id
    AND parent_job.process_id = parent.process_id
    AND parent_job.user_id = parent.user_id
  WHERE child.process_id = j.process_id
    AND child.kind = '{ACTION_SUBAGENT_PROCESS_KIND}'
    AND parent.kind = '{ACTION_PROCESS_KIND}' AND parent.status = 'running'
    AND parent_job.job_type = '{LOCAL_ACTION_JOB_TYPE}'
    AND parent_job.status = '{JOB_STATUS_RUNNING}'
    AND parent_job.completed_at IS NULL
    AND parent_job.cancel_requested_at IS NULL
    AND parent_job.logical_key = child.action_id))
"""


class ClaimedLocalJob(TypedDict):
    job_id: str
    job_type: str
    user_id: str
    process_id: str
    claimed_by: str
    payload_json: str


def claim_next_pending_job(
    *,
    db_path: str,
    busy_timeout_ms: int,
    job_type: str,
    owner_user_id: str,
    claimed_by: str,
    process_running_status: PROCESS_RUNTIME_STATUS,
    expected_process_pending_status: PROCESS_RUNTIME_STATUS,
) -> ClaimedLocalJob | None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    if not job_type.strip():
        raise MigrationError("job_type must not be empty")
    owner_user_id = owner_user_id.strip()
    if not owner_user_id:
        raise MigrationError("owner_user_id must not be empty")
    if not claimed_by.strip():
        raise MigrationError("claimed_by must not be empty")

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        while True:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT j.job_id
                FROM jobs AS j
                WHERE j.job_type = ? AND j.user_id = ? AND j.status = ?
                  AND j.scheduled_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                  {_CLAIMABLE_PARENT_SQL}
                ORDER BY j.scheduled_at ASC
                LIMIT 1
                """,
                (job_type, owner_user_id, JOB_STATUS_QUEUED),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            envelope = load_local_job_envelope(
                connection=connection,
                job_id=str(row[0]),
            )
            if envelope is None:
                connection.rollback()
                raise LocalJobEnvelopeIntegrityError(
                    "pending local job disappeared during claim"
                )
            try:
                require_valid_local_job_envelope(
                    envelope,
                    expected_job_type=job_type,
                    expected_process_status=expected_process_pending_status,
                )
            except LocalJobEnvelopeIntegrityError:
                block_invalid_local_job(connection=connection, job_id=envelope.job_id)
                connection.commit()
                raise

            claimed = _claim_row(
                connection=connection,
                envelope=envelope,
                claimed_by=claimed_by,
                process_running_status=process_running_status,
                expected_process_pending_status=expected_process_pending_status,
            )
            if claimed is not None:
                connection.commit()
                return claimed
            connection.rollback()


def finalize_local_job(
    *,
    db_path: str,
    busy_timeout_ms: int,
    job_id: str,
    final_status: FINAL_JOB_STATUS,
    process_final_status: PROCESS_RUNTIME_STATUS,
    failure_message: str | None = None,
) -> None:
    """Write one job's terminal transition, and why it failed when it did.

    ``failure_message`` is the caller's safe summary of the exception that ended
    the run. A terminal without one leaves the existing reason columns alone.
    """
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    if not job_id.strip():
        raise MigrationError("job_id must not be empty")
    if final_status not in FINALIZABLE_JOB_STATUSES:
        raise MigrationError(f"unsupported final job status: {final_status}")

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.execute("BEGIN IMMEDIATE")
        envelope = load_local_job_envelope(connection=connection, job_id=job_id)
        if envelope is None:
            connection.rollback()
            raise LocalJobEnvelopeIntegrityError("local job does not exist")
        try:
            require_valid_local_job_envelope(envelope)
        except LocalJobEnvelopeIntegrityError:
            block_invalid_local_job(connection=connection, job_id=job_id)
            connection.commit()
            raise
        if (
            envelope.process_id is None
            or envelope.process_user_id is None
            or envelope.process_kind is None
        ):
            connection.rollback()
            raise LocalJobEnvelopeIntegrityError("local job process is incomplete")
        failure_code = (
            LOCAL_JOB_EXECUTION_FAILED_ERROR_CODE
            if failure_message is not None
            else None
        )
        try:
            job_cursor = connection.execute(
                """
                UPDATE jobs
                SET
                    status = ?,
                    completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    error_code = COALESCE(?, error_code)
                WHERE job_id = ? AND user_id = ? AND job_type = ? AND process_id = ?
                """,
                (
                    final_status,
                    failure_code,
                    job_id,
                    envelope.job_user_id,
                    envelope.job_type,
                    envelope.process_id,
                ),
            )
            if int(job_cursor.rowcount) != 1:
                raise LocalJobEnvelopeIntegrityError(
                    "local job changed during finalization"
                )
            process_cursor = connection.execute(
                """
                UPDATE processes
                SET
                    status = ?,
                    current_job_id = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    completed_at = CASE
                        WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        ELSE completed_at
                    END
                WHERE process_id = ? AND user_id = ? AND kind = ?
                """,
                (
                    process_final_status,
                    int(_is_terminal_process_status(process_final_status)),
                    envelope.process_id,
                    envelope.job_user_id,
                    envelope.process_kind,
                ),
            )
            if int(process_cursor.rowcount) != 1:
                raise LocalJobEnvelopeIntegrityError(
                    "local job process changed during finalization"
                )
            connection.execute(
                """
                UPDATE job_attempts
                SET
                    status = ?,
                    completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    error_code = COALESCE(?, error_code),
                    error_message = COALESCE(?, error_message)
                WHERE job_id = ? AND status = 'running'
                """,
                (
                    _attempt_status_for(final_status),
                    failure_code,
                    failure_message,
                    job_id,
                ),
            )
            _record_job_event(
                connection,
                user_id=envelope.job_user_id,
                job_type=envelope.job_type,
                phase=_TERMINAL_PHASE[final_status],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def has_pending_job(*, db_path: str, busy_timeout_ms: int, job_type: str) -> bool:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    if not job_type.strip():
        raise MigrationError("job_type must not be empty")

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            """
            SELECT 1
            FROM jobs
            WHERE job_type = ?
              AND status = ?
              AND scheduled_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            LIMIT 1
            """,
            (job_type, JOB_STATUS_QUEUED),
        ).fetchone()
    return row is not None


def peek_next_pending_job_type(
    *,
    db_path: str,
    busy_timeout_ms: int,
    job_types: Sequence[str],
    owner_user_id: str,
) -> str | None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    if not job_types:
        raise MigrationError("job_types must not be empty")
    if any(not job_type.strip() for job_type in job_types):
        raise MigrationError("job_types must not contain empty values")
    owner_user_id = owner_user_id.strip()
    if not owner_user_id:
        raise MigrationError("owner_user_id must not be empty")

    placeholders = ",".join("?" for _ in job_types)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            f"""
            SELECT j.job_type
            FROM jobs AS j
            WHERE j.user_id = ? AND j.status = ?
              AND j.scheduled_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
              AND j.job_type IN ({placeholders})
              {_CLAIMABLE_PARENT_SQL}
            ORDER BY j.scheduled_at ASC
            LIMIT 1
            """,
            (owner_user_id, JOB_STATUS_QUEUED, *job_types),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _claim_row(
    *,
    connection: sqlite3.Connection,
    envelope: LocalJobEnvelope,
    claimed_by: str,
    process_running_status: PROCESS_RUNTIME_STATUS,
    expected_process_pending_status: PROCESS_RUNTIME_STATUS,
) -> ClaimedLocalJob | None:
    process_id = envelope.process_id
    process_kind = envelope.process_kind
    payload_json = envelope.payload_json
    if process_id is None or process_kind is None or payload_json is None:
        raise LocalJobEnvelopeIntegrityError("claimable local job is incomplete")
    next_attempt = envelope.attempt + 1
    cursor = connection.execute(
        """
        UPDATE jobs
        SET
            status = ?,
            claimed_by = ?,
            claimed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            started_at = COALESCE(started_at, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            attempt = ?
        WHERE job_id = ? AND user_id = ? AND job_type = ? AND process_id = ?
          AND status = ?
        """,
        (
            JOB_STATUS_RUNNING,
            claimed_by,
            next_attempt,
            envelope.job_id,
            envelope.job_user_id,
            envelope.job_type,
            process_id,
            JOB_STATUS_QUEUED,
        ),
    )
    if int(cursor.rowcount) != 1:
        return None
    process_cursor = connection.execute(
        """
        UPDATE processes
        SET
            status = ?,
            current_job_id = ?,
            heartbeat_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE process_id = ? AND user_id = ? AND kind = ? AND status = ?
        """,
        (
            process_running_status,
            envelope.job_id,
            process_id,
            envelope.job_user_id,
            process_kind,
            expected_process_pending_status,
        ),
    )
    if int(process_cursor.rowcount) != 1:
        return None
    connection.execute(
        """
        INSERT INTO job_attempts(
            attempt_id,
            job_id,
            attempt_number,
            started_at,
            status
        ) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'running')
        """,
        (str(uuid.uuid4()), envelope.job_id, next_attempt),
    )
    _record_job_event(
        connection,
        user_id=envelope.job_user_id,
        job_type=envelope.job_type,
        phase="started",
    )
    return {
        "job_id": envelope.job_id,
        "job_type": envelope.job_type,
        "user_id": envelope.job_user_id,
        "process_id": process_id,
        "claimed_by": claimed_by,
        "payload_json": payload_json,
    }


def block_invalid_local_job(*, connection: sqlite3.Connection, job_id: str) -> None:
    """Quarantine one job whose persisted identity cannot be trusted."""

    connection.execute(
        """
        UPDATE jobs
        SET status = 'blocked', error_code = ?,
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE job_id = ?
          AND status IN ('queued', 'running', 'paused', 'retryable_error')
        """,
        (LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR_CODE, job_id),
    )
    connection.execute(
        """
        UPDATE job_attempts
        SET status = 'failed', error_code = ?,
            error_message = 'Persisted job and process identity do not match.',
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE job_id = ? AND status = 'running'
        """,
        (LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR_CODE, job_id),
    )


# Abandoned and blocked jobs ended without their work done, as a failed one did.
_TERMINAL_PHASE: dict[FINAL_JOB_STATUS, JobPhase] = {
    "completed": "completed",
    "failed": "failed",
    "abandoned": "failed",
    "blocked": "failed",
    "canceled": "canceled",
}


def _record_job_event(
    connection: sqlite3.Connection, *, user_id: str, job_type: str, phase: JobPhase
) -> None:
    record_event(
        connection,
        user_id=user_id,
        event=JobLifecycleEvent(job_type=job_type, phase=phase),
        occurred_at=now_utc_iso(),
    )


def _attempt_status_for(final_status: FINAL_JOB_STATUS) -> str:
    if final_status == "completed":
        return "completed"
    if final_status in {"failed", "abandoned", "blocked", "canceled"}:
        return "failed" if final_status != "canceled" else "canceled"
    raise MigrationError(f"unsupported final job status: {final_status}")


def _is_terminal_process_status(status: PROCESS_RUNTIME_STATUS) -> bool:
    return status in PROCESS_TERMINAL_STATUSES

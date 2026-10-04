from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso, utc_now
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .connection import open_memory_catalog_connection

MEMORY_REPAIR_INITIAL_BACKOFF_SECONDS = 60
MEMORY_REPAIR_MAX_BACKOFF_SECONDS = 3_600


@dataclass(frozen=True, slots=True)
class RepairJob:
    user_id: str
    node_id: str
    detected_revision_id: str
    reason: str
    attempt_count: int


def list_due_repair_jobs(
    *, db_path: Path, busy_timeout_ms: int, limit: int, now: str
) -> tuple[RepairJob, ...]:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        rows = connection.execute(
            """
            SELECT user_id, node_id, detected_revision_id, reason, attempt_count
            FROM memory_repair_queue WHERE state = 'pending'
              AND julianday(next_attempt_at) <= julianday(?)
            ORDER BY julianday(next_attempt_at), created_at, node_id LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    return tuple(
        RepairJob(
            user_id=str(row["user_id"]),
            node_id=str(row["node_id"]),
            detected_revision_id=str(row["detected_revision_id"]),
            reason=str(row["reason"]),
            attempt_count=int(row["attempt_count"]),
        )
        for row in rows
    )


def complete_repair_job(
    *, connection: sqlite3.Connection, job: RepairJob, completed_at: str
) -> None:
    connection.execute(
        """
        UPDATE memory_repair_queue SET state = 'completed', completed_at = ?
        WHERE user_id = ? AND node_id = ? AND detected_revision_id = ? AND reason = ?
          AND state = 'pending'
        """,
        (
            completed_at,
            job.user_id,
            job.node_id,
            job.detected_revision_id,
            job.reason,
        ),
    )


def schedule_repair_retry(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    job: RepairJob,
    error_code: str,
) -> None:
    delay_seconds = _retry_delay_seconds(job.attempt_count)
    next_attempt_at = format_utc_iso(utc_now() + timedelta(seconds=delay_seconds))
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            connection.execute(
                """
                UPDATE memory_repair_queue
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?, last_error_code = ?
                WHERE user_id = ? AND node_id = ? AND detected_revision_id = ?
                  AND reason = ? AND state = 'pending'
                """,
                (
                    next_attempt_at,
                    error_code,
                    job.user_id,
                    job.node_id,
                    job.detected_revision_id,
                    job.reason,
                ),
            )


def _retry_delay_seconds(attempt_count: int) -> int:
    delay_seconds = MEMORY_REPAIR_INITIAL_BACKOFF_SECONDS
    remaining_attempts = attempt_count
    while remaining_attempts > 0 and delay_seconds < MEMORY_REPAIR_MAX_BACKOFF_SECONDS:
        delay_seconds = min(
            delay_seconds * 2,
            MEMORY_REPAIR_MAX_BACKOFF_SECONDS,
        )
        remaining_attempts -= 1
    return delay_seconds

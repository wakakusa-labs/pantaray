from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from .utc_timestamps import format_utc_iso, now_utc_iso

_SCHEDULER_RESTART_ERROR_CODE = "SCHEDULER_PROCESS_RESTARTED"


def recover_interrupted_activity_summary_scheduler_runs(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    recovered_at: datetime | None = None,
) -> int:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    completed_at = (
        now_utc_iso() if recovered_at is None else format_utc_iso(recovered_at)
    )
    error_details_json = json.dumps(
        {"message": "Activity Summary scheduler process restarted"},
        ensure_ascii=False,
    )
    with sqlite3.connect(str(db_path)) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """
                UPDATE scheduler_job_runs
                SET status = 'failed', error_code = ?, error_details_json = ?,
                    completed_at = ?
                WHERE status = 'running'
                  AND job_name IN (
                      SELECT job_name
                      FROM scheduler_jobs
                      WHERE job_kind = 'activity_summary'
                  )
                """,
                (
                    _SCHEDULER_RESTART_ERROR_CODE,
                    error_details_json,
                    completed_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return max(cursor.rowcount, 0)


__all__ = ["recover_interrupted_activity_summary_scheduler_runs"]

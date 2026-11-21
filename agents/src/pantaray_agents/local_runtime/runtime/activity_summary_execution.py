from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast

from pantaray_agents.local_runtime.storage.migrations import MigrationError

from .activity_source_db import with_activity_source_connection
from .activity_source_rows import (
    ACTIVITY_SOURCE_STATUS_PROCESSING,
    ACTIVITY_SOURCE_TERMINAL_STATUSES,
)

ActivitySummaryExecutionState = Literal[
    "processing",
    "success",
    "error",
    "canceled",
    "timeout",
]

# How late a window may still be summarized. Once a connection or a restart
# brings the runtime back, the periods it slept through are no longer worth an
# LLM request, and walking them would hold back the summary of the period the
# user is actually in. Taking the larger of this and the window's own length
# keeps every type above 1h able to produce its last complete period even when
# nothing was running right after that period's boundary.
ACTIVITY_SUMMARY_MAX_WINDOW_LAG: Final[timedelta] = timedelta(hours=24)


def is_activity_summary_window_expired(
    *,
    period_start: datetime,
    period_end: datetime,
    now: datetime,
) -> bool:
    """Report whether this window is now too old to summarize.

    The enqueue side (the scheduler picking a window) and the execution side (a
    claimed job) share this one rule: a job that waited in the queue while there
    was no route is expired by the time it is claimed, and ends without an LLM
    request.
    """
    return now - period_end >= max(
        ACTIVITY_SUMMARY_MAX_WINDOW_LAG, period_end - period_start
    )


def load_activity_summary_execution_state(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    summary_id: str,
    user_id: str,
    summary_type: str,
    period_start: str,
    period_end: str,
) -> ActivitySummaryExecutionState:
    state: ActivitySummaryExecutionState | None = None

    def _load(connection: sqlite3.Connection) -> None:
        nonlocal state
        row = connection.execute(
            """
            SELECT user_id, summary_type, period_start, period_end,
                   status, updated_at
            FROM activity_summaries
            WHERE summary_id = ?
            """,
            (summary_id,),
        ).fetchone()
        if row is None:
            raise MigrationError(
                f"activity_summary execution row not found: summary_id={summary_id}"
            )
        if tuple(str(value) for value in row[:4]) != (
            user_id,
            summary_type,
            period_start,
            period_end,
        ):
            raise MigrationError(
                f"activity_summary execution identity mismatch: summary_id={summary_id}"
            )
        status = str(row[4])
        valid_statuses = ACTIVITY_SOURCE_TERMINAL_STATUSES | {
            ACTIVITY_SOURCE_STATUS_PROCESSING
        }
        if status not in valid_statuses:
            raise MigrationError(
                f"activity_summary execution status is invalid: summary_id={summary_id}"
            )
        state = cast(ActivitySummaryExecutionState, status)

    with_activity_source_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        operation=_load,
        transactional=False,
    )
    if state is None:
        raise MigrationError(
            f"activity_summary execution state was not loaded: summary_id={summary_id}"
        )
    return state


__all__ = [
    "ACTIVITY_SUMMARY_MAX_WINDOW_LAG",
    "ActivitySummaryExecutionState",
    "is_activity_summary_window_expired",
    "load_activity_summary_execution_state",
]

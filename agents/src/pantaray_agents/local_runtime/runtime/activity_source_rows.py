from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.hooks.events import ActivitySummaryRecorded
from pantaray_agents.local_runtime.hooks.inbox import record_event
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_activity_summary_memory,
)
from pantaray_agents.local_runtime.runtime.activity_source_db import (
    with_activity_source_connection,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.schema.agent.base import AgentError, ErrorSeverity, ErrorType

ACTIVITY_SOURCE_STATUS_PROCESSING: Final[str] = "processing"
ACTIVITY_SOURCE_STATUS_SUCCESS: Final[str] = "success"
ACTIVITY_SOURCE_STATUS_ERROR: Final[str] = "error"
ACTIVITY_SOURCE_STATUS_CANCELED: Final[str] = "canceled"
ACTIVITY_SOURCE_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"success", "error", ACTIVITY_SOURCE_STATUS_CANCELED, "timeout"}
)
ACTIVITY_SUMMARY_PROMPT_NAME: Final[str] = "activity_summary"
ACTIVITY_SUMMARY_PROMPT_VERSION: Final[str] = "1.0"


def reserve_activity_summary_processing(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    summary_id: str,
    user_id: str,
    summary_type: str,
    period_start: str,
    period_end: str,
    created_at: str,
) -> None:
    with_activity_source_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        operation=lambda connection: reserve_activity_summary_processing_in_connection(
            connection=connection,
            summary_id=summary_id,
            user_id=user_id,
            summary_type=summary_type,
            period_start=period_start,
            period_end=period_end,
            created_at=created_at,
        ),
    )


def finalize_activity_summary_start_error_if_processing(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    summary_id: str,
    user_id: str,
    updated_at: str,
    error_code: str,
    error_message: str,
) -> None:
    with_activity_source_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        operation=lambda connection: _finalize_activity_summary_error_if_processing(
            connection=connection,
            summary_id=summary_id,
            user_id=user_id,
            updated_at=updated_at,
            error_code=error_code,
            error_message=error_message,
        ),
    )


def persist_activity_summary_result(
    *,
    db_path: Path | str,
    busy_timeout_ms: int,
    summary_id: str,
    user_id: str,
    summary_type: str,
    period_start: str,
    period_end: str,
    summary: str,
    status: str,
    error: AgentError | None,
    prompt_text: str,
    thinking: str | None,
    source_ids: list[str],
    updated_at: str,
) -> None:
    def _persist(connection: sqlite3.Connection) -> None:
        if status not in ACTIVITY_SOURCE_TERMINAL_STATUSES:
            raise MigrationError("activity_summary result must be terminal")
        serialized_error = _serialize_error(error)
        serialized_source_ids = json.dumps(source_ids, ensure_ascii=False)
        existing = connection.execute(
            """
            SELECT user_id, summary_type, period_start, period_end, summary, status,
                   error, prompt_text, thinking, prompt_name, prompt_version,
                   source_ids, updated_at
            FROM activity_summaries
            WHERE summary_id = ?
            """,
            (summary_id,),
        ).fetchone()
        if existing is None:
            raise MigrationError(
                "activity_summary result row not found for "
                f"summary_id={summary_id} user_id={user_id}"
            )
        existing_status = str(existing[5])
        expected_values = (
            user_id,
            summary_type,
            period_start,
            period_end,
            summary,
            status,
            serialized_error,
            prompt_text,
            thinking,
            ACTIVITY_SUMMARY_PROMPT_NAME,
            ACTIVITY_SUMMARY_PROMPT_VERSION,
            serialized_source_ids,
        )
        if existing_status != ACTIVITY_SOURCE_STATUS_PROCESSING:
            if tuple(existing[:12]) != expected_values:
                raise MigrationError(
                    "activity_summary terminal row cannot be overwritten"
                )
            return

        cursor = connection.execute(
            """
            UPDATE activity_summaries
            SET summary = ?,
                status = ?,
                error = ?,
                prompt_text = ?,
                thinking = ?,
                prompt_name = ?,
                prompt_version = ?,
                source_ids = ?,
                updated_at = ?
            WHERE summary_id = ?
              AND user_id = ?
              AND summary_type = ?
              AND period_start = ?
              AND period_end = ?
              AND status = ?
            """,
            (
                summary,
                status,
                serialized_error,
                prompt_text,
                thinking,
                ACTIVITY_SUMMARY_PROMPT_NAME,
                ACTIVITY_SUMMARY_PROMPT_VERSION,
                serialized_source_ids,
                updated_at,
                summary_id,
                user_id,
                summary_type,
                period_start,
                period_end,
                ACTIVITY_SOURCE_STATUS_PROCESSING,
            ),
        )
        if cursor.rowcount != 1:
            raise MigrationError("activity_summary terminal transition lost its owner")
        if status == ACTIVITY_SOURCE_STATUS_SUCCESS:
            register_activity_summary_memory(
                connection=connection,
                user_id=user_id,
                summary_id=summary_id,
                content=summary,
                source_ids=tuple(source_ids),
            )
            record_event(
                connection,
                user_id=user_id,
                event=ActivitySummaryRecorded(
                    summary_id=summary_id, summary_type=summary_type
                ),
                occurred_at=updated_at,
            )

    with_activity_source_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        operation=_persist,
    )


def reserve_activity_summary_processing_in_connection(
    *,
    connection: sqlite3.Connection,
    summary_id: str,
    user_id: str,
    summary_type: str,
    period_start: str,
    period_end: str,
    created_at: str,
) -> None:
    ensure_user_row(connection, user_id=user_id, timestamp=created_at)
    try:
        connection.execute(
            """
            INSERT INTO activity_summaries(
                summary_id,
                user_id,
                summary_type,
                period_start,
                period_end,
                summary,
                status,
                error,
                prompt_text,
                prompt_name,
                prompt_version,
                source_ids,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, '', ?, NULL, '', ?, ?, '[]', ?, ?)
            """,
            (
                summary_id,
                user_id,
                summary_type,
                period_start,
                period_end,
                ACTIVITY_SOURCE_STATUS_PROCESSING,
                ACTIVITY_SUMMARY_PROMPT_NAME,
                ACTIVITY_SUMMARY_PROMPT_VERSION,
                created_at,
                created_at,
            ),
        )
    except sqlite3.IntegrityError:
        row = connection.execute(
            """
            SELECT user_id, summary_type, period_start, period_end
            FROM activity_summaries
            WHERE summary_id = ?
            """,
            (summary_id,),
        ).fetchone()
        if row is None:
            raise
        if (
            str(row[0]) != user_id
            or str(row[1]) != summary_type
            or str(row[2]) != period_start
            or str(row[3]) != period_end
        ):
            raise MigrationError(
                f"activity_summary identity mismatch for existing summary_id: {summary_id}"
            ) from None


def _finalize_activity_summary_error_if_processing(
    *,
    connection: sqlite3.Connection,
    summary_id: str,
    user_id: str,
    updated_at: str,
    error_code: str,
    error_message: str,
) -> None:
    connection.execute(
        """
        UPDATE activity_summaries
        SET status = ?,
            error = ?,
            updated_at = ?
        WHERE summary_id = ? AND user_id = ? AND status = ?
        """,
        (
            ACTIVITY_SOURCE_STATUS_ERROR,
            _serialize_error(
                AgentError(
                    error_type=ErrorType.INTERNAL_ERROR.value,
                    error_code=error_code,
                    error_message=error_message,
                    error_details=None,
                    severity=ErrorSeverity.ERROR.value,
                    metadata={"stage": "enqueue_local_job"},
                )
            ),
            updated_at,
            summary_id,
            user_id,
            ACTIVITY_SOURCE_STATUS_PROCESSING,
        ),
    )


def _serialize_error(error: AgentError | None) -> str | None:
    if error is None:
        return None
    return error.model_dump_json()

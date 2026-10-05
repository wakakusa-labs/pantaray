from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from ..storage.migrations import MigrationError

BUSY_TIMEOUT_PRAGMA_TEMPLATE = "PRAGMA busy_timeout = {timeout_ms};"
FOREIGN_KEYS_ON_PRAGMA = "PRAGMA foreign_keys = ON;"


def _configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    connection.row_factory = sqlite3.Row
    connection.execute(BUSY_TIMEOUT_PRAGMA_TEMPLATE.format(timeout_ms=busy_timeout_ms))
    connection.execute(FOREIGN_KEYS_ON_PRAGMA)


def create_runtime_lock_resource(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    lock_path: Path,
    lock_id: str,
    owner_pid: int,
    created_at: str,
) -> str:
    resource_id = str(uuid.uuid4())
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            connection.execute(
                """
                INSERT INTO runtime_lock_resources(
                    resource_id,
                    lock_path,
                    lock_id,
                    owner_pid,
                    status,
                    created_at,
                    updated_at,
                    cleaned_at,
                    cleanup_error,
                    cleanup_attempts
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, NULL, 0)
                """,
                (
                    resource_id,
                    str(lock_path),
                    lock_id,
                    owner_pid,
                    created_at,
                    created_at,
                ),
            )
    return resource_id


def mark_runtime_lock_resource_cleaned(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    resource_id: str,
    cleaned_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            cursor = connection.execute(
                """
                UPDATE runtime_lock_resources
                SET status = 'cleaned', updated_at = ?, cleaned_at = ?, cleanup_error = NULL
                WHERE resource_id = ?
                """,
                (cleaned_at, cleaned_at, resource_id),
            )
        if cursor.rowcount != 1:
            raise MigrationError(
                "runtime lock resource status update failed: expected exactly one row"
            )


def list_open_runtime_lock_resource_ids(
    *,
    db_path: Path,
    busy_timeout_ms: int,
) -> tuple[str, ...]:
    # 'cleanup_failed' rows were written by releases that predate the flock lock.
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        try:
            rows = connection.execute(
                """
                SELECT resource_id
                FROM runtime_lock_resources
                WHERE status IN ('active', 'cleanup_failed')
                ORDER BY created_at ASC
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: runtime_lock_resources" in str(exc):
                return ()
            raise
    return tuple(str(row["resource_id"]) for row in rows)


def record_runtime_lock_event(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    resource_id: str | None,
    event_type: str,
    message: str,
    created_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            connection.execute(
                """
                INSERT INTO runtime_lock_events(
                    event_id,
                    resource_id,
                    event_type,
                    message,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), resource_id, event_type, message, created_at),
            )


__all__ = [
    "create_runtime_lock_resource",
    "list_open_runtime_lock_resource_ids",
    "mark_runtime_lock_resource_cleaned",
    "record_runtime_lock_event",
]

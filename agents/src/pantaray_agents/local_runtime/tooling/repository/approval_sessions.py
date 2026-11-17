from __future__ import annotations

import sqlite3
from pathlib import Path

from ...storage.migrations import MigrationError
from ...storage.transactions import immediate_transaction
from ...storage.users import ensure_user_row
from ..models import (
    ApprovalSessionUpsertInput,
    StoredApprovalSession,
)
from .common import (
    _configure_connection,
    _deserialize_json_string_map,
    _serialize_json,
)


class ApprovalSessionExecutionConflictError(MigrationError):
    """Raised when approval creation cannot bind to its live session."""


def upsert_approval_session(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    approval_session: ApprovalSessionUpsertInput,
) -> StoredApprovalSession:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with immediate_transaction(connection):
            ensure_user_row(
                connection,
                user_id=approval_session.user_id,
                timestamp=approval_session.requested_at,
            )
            if approval_session.tool_invocation_id is not None:
                invocation_row = connection.execute(
                    "SELECT invocation_id FROM tool_invocations WHERE invocation_id = ?",
                    (approval_session.tool_invocation_id,),
                ).fetchone()
                if invocation_row is None:
                    raise MigrationError(
                        "approval session requires an existing tool invocation"
                    )
            live_owner = connection.execute(
                """
                SELECT 1
                FROM execution_sessions AS session
                JOIN workspace_manifests AS manifest ON manifest.manifest_id = ?
                WHERE session.execution_session_id = ?
                  AND session.status = 'running'
                  AND session.user_id = ?
                  AND session.action_id = ?
                  AND manifest.status = 'ready'
                  AND manifest.user_id = session.user_id
                  AND manifest.action_id = session.action_id
                  AND manifest.execution_session_id = session.execution_session_id
                """,
                (
                    approval_session.manifest_id,
                    approval_session.expected_execution_session_id,
                    approval_session.user_id,
                    approval_session.action_id,
                ),
            ).fetchone()
            if live_owner is None:
                raise ApprovalSessionExecutionConflictError(
                    "approval session creation rejected: execution session must be "
                    "running and exactly match the approval owner and current ready "
                    "manifest"
                )
            existing_session = _load_approval_session_by_request_in_connection(
                connection=connection,
                user_id=approval_session.user_id,
                tool_request_id=approval_session.tool_request_id,
            )
            if existing_session is None:
                connection.execute(
                    """
                    INSERT INTO approval_sessions(
                        approval_session_id,
                        user_id,
                        action_id,
                        manifest_id,
                        tool_invocation_id,
                        tool_id,
                        intent_class,
                        approval_source,
                        status,
                        approved_capabilities_json,
                        command_summary_json,
                        requested_at,
                        decided_at,
                        created_at,
                        tool_request_id,
                        claimed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_session.approval_session_id,
                        approval_session.user_id,
                        approval_session.action_id,
                        approval_session.manifest_id,
                        approval_session.tool_invocation_id,
                        approval_session.tool_id,
                        approval_session.intent_class,
                        approval_session.approval_source,
                        approval_session.status,
                        _serialize_json(approval_session.approved_capabilities_json),
                        _serialize_json(approval_session.command_summary_json),
                        approval_session.requested_at,
                        approval_session.decided_at,
                        approval_session.requested_at,
                        approval_session.tool_request_id,
                        approval_session.claimed_at,
                    ),
                )
            else:
                _assert_same_approval_request_identity(
                    existing=existing_session,
                    incoming=approval_session,
                )
            stored_session = _load_approval_session_by_request_in_connection(
                connection=connection,
                user_id=approval_session.user_id,
                tool_request_id=approval_session.tool_request_id,
            )
            if stored_session is None:
                raise MigrationError("approval session upsert did not persist a row")
            return stored_session


def load_latest_approval_session(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    manifest_id: str,
    tool_request_id: str,
    tool_id: str,
) -> StoredApprovalSession | None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        row = connection.execute(
            """
            SELECT
                approval_session_id,
                user_id,
                action_id,
                manifest_id,
                tool_invocation_id,
                tool_id,
                intent_class,
                approval_source,
                review_reason,
                tool_request_id,
                status,
                approved_capabilities_json,
                command_summary_json,
                decided_at,
                claimed_at
            FROM approval_sessions
            WHERE
                user_id = ?
                AND manifest_id = ?
                AND tool_request_id = ?
                AND tool_id = ?
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            (user_id, manifest_id, tool_request_id, tool_id),
        ).fetchone()
    if row is None:
        return None
    return _build_stored_approval_session(row)


def load_approval_session_by_request(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool_request_id: str,
) -> StoredApprovalSession | None:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        return _load_approval_session_by_request_in_connection(
            connection=connection,
            user_id=user_id,
            tool_request_id=tool_request_id,
        )


def load_approval_session_by_id(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    approval_session_id: str,
) -> StoredApprovalSession:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        row = connection.execute(
            """
            SELECT
                approval_session_id,
                user_id,
                action_id,
                manifest_id,
                tool_invocation_id,
                tool_id,
                intent_class,
                approval_source,
                review_reason,
                tool_request_id,
                status,
                approved_capabilities_json,
                command_summary_json,
                decided_at,
                claimed_at
            FROM approval_sessions
            WHERE approval_session_id = ?
            """,
            (approval_session_id,),
        ).fetchone()
    if row is None:
        raise MigrationError(f"approval session not found: {approval_session_id}")
    return _build_stored_approval_session(row)


def _build_stored_approval_session(row: sqlite3.Row) -> StoredApprovalSession:
    return StoredApprovalSession(
        approval_session_id=str(row["approval_session_id"]),
        user_id=str(row["user_id"]),
        action_id=str(row["action_id"]),
        manifest_id=str(row["manifest_id"]),
        claimed_at=str(row["claimed_at"]) if row["claimed_at"] is not None else None,
        tool_invocation_id=(
            str(row["tool_invocation_id"])
            if row["tool_invocation_id"] is not None
            else None
        ),
        tool_id=str(row["tool_id"]),
        intent_class=str(row["intent_class"]),
        approval_source=str(row["approval_source"]),
        review_reason=row["review_reason"],
        tool_request_id=str(row["tool_request_id"]),
        status=str(row["status"]),
        approved_capabilities_json=_deserialize_json_string_map(
            row["approved_capabilities_json"],
            field_name="approved_capabilities_json",
        ),
        command_summary_json=_deserialize_json_string_map(
            row["command_summary_json"], field_name="command_summary_json"
        ),
        decided_at=str(row["decided_at"]) if row["decided_at"] is not None else None,
    )


def _load_approval_session_by_request_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    tool_request_id: str,
) -> StoredApprovalSession | None:
    row = connection.execute(
        """
        SELECT
            approval_session_id,
            user_id,
            action_id,
            manifest_id,
            tool_invocation_id,
            tool_id,
            intent_class,
            approval_source,
            review_reason,
            tool_request_id,
            status,
            approved_capabilities_json,
            command_summary_json,
            decided_at,
            claimed_at
        FROM approval_sessions
        WHERE user_id = ? AND tool_request_id = ?
        """,
        (user_id, tool_request_id),
    ).fetchone()
    if row is None:
        return None
    return _build_stored_approval_session(row)


def _assert_same_approval_request_identity(
    *,
    existing: StoredApprovalSession,
    incoming: ApprovalSessionUpsertInput,
) -> None:
    if (
        existing.action_id != incoming.action_id
        or existing.manifest_id != incoming.manifest_id
        or existing.tool_id != incoming.tool_id
        or existing.intent_class != incoming.intent_class
        or existing.approval_source != incoming.approval_source
        or _serialize_json(existing.approved_capabilities_json)
        != _serialize_json(incoming.approved_capabilities_json)
        or _serialize_json(existing.command_summary_json)
        != _serialize_json(incoming.command_summary_json)
    ):
        raise MigrationError(
            "approval session identity mismatch for existing tool_request_id"
        )

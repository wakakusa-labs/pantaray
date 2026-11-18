from __future__ import annotations

import json
import sqlite3

from pantaray_agents.schema.agent.base import JSONValue

from ...storage.migrations import MigrationError
from ..models import StoredToolRuntimeResource, StoredToolRuntimeResourceEvent

BUSY_TIMEOUT_PRAGMA_TEMPLATE = "PRAGMA busy_timeout = {timeout_ms};"
FOREIGN_KEYS_ON_PRAGMA = "PRAGMA foreign_keys = ON;"
TOOL_INVOCATION_INFLIGHT_STATUSES: tuple[str, ...] = ("queued", "running")


def configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    connection.row_factory = sqlite3.Row
    connection.execute(BUSY_TIMEOUT_PRAGMA_TEMPLATE.format(timeout_ms=busy_timeout_ms))
    connection.execute(FOREIGN_KEYS_ON_PRAGMA)


def serialize_json(payload: JSONValue) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_tool_runtime_resource(row: sqlite3.Row) -> StoredToolRuntimeResource:
    return StoredToolRuntimeResource(
        resource_id=str(row["resource_id"]),
        execution_session_id=str(row["execution_session_id"]),
        tool_invocation_id=(
            str(row["tool_invocation_id"])
            if row["tool_invocation_id"] is not None
            else None
        ),
        action_id=str(row["action_id"]) if row["action_id"] is not None else None,
        resource_kind=str(row["resource_kind"]),
        status=str(row["status"]),
        pid=int(row["pid"]) if row["pid"] is not None else None,
        pgid=int(row["pgid"]) if row["pgid"] is not None else None,
        process_start_signature=(
            str(row["process_start_signature"])
            if row["process_start_signature"] is not None
            else None
        ),
        resource_path=str(row["resource_path"])
        if row["resource_path"] is not None
        else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        cleaned_at=str(row["cleaned_at"]) if row["cleaned_at"] is not None else None,
        cleanup_error=str(row["cleanup_error"])
        if row["cleanup_error"] is not None
        else None,
        cleanup_attempts=int(row["cleanup_attempts"]),
        lock_id=str(row["lock_id"]) if row["lock_id"] is not None else None,
        owner_process_id=(
            str(row["owner_process_id"])
            if row["owner_process_id"] is not None
            else None
        ),
    )


def build_tool_runtime_resource_event(
    row: sqlite3.Row,
) -> StoredToolRuntimeResourceEvent:
    return StoredToolRuntimeResourceEvent(
        event_id=str(row["event_id"]),
        resource_id=str(row["resource_id"]) if row["resource_id"] is not None else None,
        tool_invocation_id=(
            str(row["tool_invocation_id"])
            if row["tool_invocation_id"] is not None
            else None
        ),
        action_id=str(row["action_id"]) if row["action_id"] is not None else None,
        event_type=str(row["event_type"]),
        message=str(row["message"]),
        created_at=str(row["created_at"]),
    )

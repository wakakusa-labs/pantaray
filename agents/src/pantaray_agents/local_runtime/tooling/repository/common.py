from __future__ import annotations

import json
import sqlite3
from typing import cast

from pantaray_agents.schema.agent.base import JSONValue

from ...storage.migrations import MigrationError

BUSY_TIMEOUT_PRAGMA_TEMPLATE = "PRAGMA busy_timeout = {timeout_ms};"
FOREIGN_KEYS_ON_PRAGMA = "PRAGMA foreign_keys = ON;"
WORKSPACE_EDIT_AND_COMMAND_SCOPE = "workspace_edit_and_command"


class ApprovalDecisionConflictError(MigrationError):
    """Approval decision CAS failed because the session was no longer pending."""


def _configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    connection.row_factory = sqlite3.Row
    connection.execute(BUSY_TIMEOUT_PRAGMA_TEMPLATE.format(timeout_ms=busy_timeout_ms))
    connection.execute(FOREIGN_KEYS_ON_PRAGMA)


def _serialize_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _deserialize_json_string_map(
    payload: str | None, *, field_name: str
) -> dict[str, JSONValue]:
    if payload is None:
        raise MigrationError(f"{field_name} must not be NULL")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise MigrationError(f"{field_name} must be a JSON object")
    return cast(dict[str, JSONValue], parsed)


def _deserialize_string_list(payload: object, *, field_name: str) -> tuple[str, ...]:
    raw = json.loads(str(payload))
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise MigrationError(f"{field_name} must be a string array")
    return tuple(raw)

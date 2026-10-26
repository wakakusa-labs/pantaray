from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.process_events import (
    append_process_event_in_connection,
)
from pantaray_agents.schema.agent.suggestion import SUGGESTION_ARTIFACTS_ADAPTER
from pantaray_agents.schema.repositories.repository import DBRow
from pantaray_agents.suggestion_reactions import parse_stored_suggestion_user_reaction

from ..storage.migrations import MigrationError
from .boolean_flags import (
    normalize_optional_sqlite_bool,
    normalize_required_sqlite_bool,
)
from .public_process_events import append_public_process_event
from .public_projection import (
    FoldedSuggestionState,
    rebuild_history_projection,
)

BUSY_TIMEOUT_PRAGMA_TEMPLATE = "PRAGMA busy_timeout = {timeout_ms};"
FOREIGN_KEYS_ON_PRAGMA = "PRAGMA foreign_keys = ON;"
ACTION_INTERACTION_CONTRACT = "action_offer"
REACTION_ACCEPTED = "accepted"
ACTION_STATUS_ERROR = "error"
LOCAL_ACTION_QUEUE_NAME = "local-action-jobs"
JOB_STATUS_PENDING = "queued"
JOB_STATUS_PAUSED = "paused"
JOB_STATUS_FAILED = "retryable_error"
PROCESS_KIND_ACTION = "action"
PROCESS_STATUS_ENQUEUED = "enqueued"
PROCESS_STATUS_RUNNING = "running"
PROCESS_STATUS_PAUSED = "paused"


def configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    connection.row_factory = sqlite3.Row
    connection.execute(BUSY_TIMEOUT_PRAGMA_TEMPLATE.format(timeout_ms=busy_timeout_ms))
    connection.execute(FOREIGN_KEYS_ON_PRAGMA)


def decode_json_object(raw_value: object) -> dict[str, object] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise MigrationError("expected JSON text column to be a string")
    decoded = json.loads(raw_value)
    if not isinstance(decoded, dict):
        raise MigrationError("expected JSON object payload")
    return decoded


def normalize_suggestion_row(row: sqlite3.Row) -> DBRow:
    result: DBRow = dict(row)
    error_json = result.pop("error", None)
    action_request_payload = result.get("action_request_payload")
    target_context_json = result.get("target_context_json")
    result["has_suggestion"] = normalize_optional_sqlite_bool(
        result.get("has_suggestion"),
        field_name="agent_suggestions.has_suggestion",
    )
    if "action_request_payload_present" in result:
        result["action_request_payload_present"] = normalize_required_sqlite_bool(
            result.get("action_request_payload_present"),
            field_name="agent_suggestion_history.action_request_payload_present",
        )
    if "user_reaction" in result:
        result["user_reaction"] = parse_stored_suggestion_user_reaction(
            result.get("user_reaction")
        )
    result["action_request_payload"] = (
        decode_json_object(action_request_payload)
        if action_request_payload is not None
        else None
    )
    result["target_context_json"] = (
        decode_json_object(target_context_json)
        if target_context_json is not None
        else None
    )
    result["error"] = decode_json_object(error_json) if error_json is not None else None
    result["artifacts_json"] = SUGGESTION_ARTIFACTS_ADAPTER.validate_json(
        result["artifacts_json"]
    )
    return result


def normalize_action_row(row: sqlite3.Row) -> DBRow:
    result: DBRow = dict(row)
    error_json = result.pop("error", None)
    execution_target_json = result.pop("execution_target_json", None)
    result["error"] = decode_json_object(error_json) if error_json is not None else None
    result["execution_target_json"] = (
        decode_json_object(execution_target_json)
        if execution_target_json is not None
        else None
    )
    return result


def serialize_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def append_internal_process_event(
    *,
    connection: sqlite3.Connection,
    process_id: str,
    event_id: str,
    event_name: str,
    payload: dict[str, object],
    created_at: str,
) -> None:
    append_process_event_in_connection(
        connection=connection,
        process_id=process_id,
        event_id=event_id,
        event_name=event_name,
        payload=payload,
        created_at=created_at,
    )


class LocalSuggestionStateRepositoryBase:
    def __init__(self, *, db_path: Path, busy_timeout_ms: int) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        configure_connection(connection, self._busy_timeout_ms)
        return connection

    @classmethod
    def _rebuild_history_projection(
        cls,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        suggestion_id: str,
    ) -> FoldedSuggestionState:
        return rebuild_history_projection(
            connection=connection,
            user_id=user_id,
            suggestion_id=suggestion_id,
        )

    @classmethod
    def _append_public_process_event(
        cls,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        process_id: str,
        suggestion_id: str,
        user_id: str,
        action_id: str | None,
        event_name: str,
        payload: dict[str, object],
        created_at: str | None = None,
    ) -> int:
        return append_public_process_event(
            connection=connection,
            event_id=event_id,
            suggestion_id=suggestion_id,
            user_id=user_id,
            action_id=action_id,
            event_name=event_name,
            payload=payload,
            created_at=created_at,
        )

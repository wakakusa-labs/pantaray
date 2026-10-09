from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from ..storage.transactions import immediate_transaction
from .job_types import LOCAL_ACTION_JOB_TYPE
from .process_events import append_process_event_in_connection
from .utc_timestamps import now_utc_iso

ACTION_SUBAGENT_MESSAGE_MAX_CODEPOINTS = 8_000
ACTION_SUBAGENT_MESSAGE_ID_MAX_CODEPOINTS = 128
ACTION_SUBAGENT_MESSAGE_EVENT = "action_subagent_message"
# The answer to one call the child made.
ACTION_SUBAGENT_TOOL_EVENT = "action_subagent_tool"
# Every parent message before it went into the child's conversation here.
ACTION_SUBAGENT_DELIVERED_EVENT = "action_subagent_messages_delivered"
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})


@dataclass(frozen=True, slots=True)
class ActionSubagentEvent:
    """One row of the child's private history, in the order it was appended."""

    event_name: str
    payload: dict[str, JSONValue]


class ActionSubagentMessageAuthorityError(RuntimeError):
    pass


class ActionSubagentMessageInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionSubagentMessageRequest:
    user_id: str
    action_id: str
    parent_process_id: str
    parent_job_id: str
    child_process_id: str
    message_id: str
    content: str


def send_action_subagent_message(
    *, db_path: Path, busy_timeout_ms: int, request: ActionSubagentMessageRequest
) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
    _validate_request(request)
    event_id = f"subagent-message:{request.message_id}"
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            child_status = _require_authority(connection, request)
            existing = connection.execute(
                "SELECT event_name,payload_json FROM process_events "
                "WHERE process_id=? AND event_id=?",
                (request.child_process_id, event_id),
            ).fetchone()
            if existing is not None:
                payload = _object(existing["payload_json"])
                bound_name = existing["event_name"]
                if bound_name != ACTION_SUBAGENT_MESSAGE_EVENT or payload != {
                    "message_id": request.message_id,
                    "content": request.content,
                }:
                    raise ActionSubagentMessageInputError(
                        "message_id is already bound to different content"
                    )
            elif child_status in _TERMINAL_STATUSES:
                raise ActionSubagentMessageInputError(
                    "terminal Action subagent cannot accept a new message"
                )
            else:
                append_process_event_in_connection(
                    connection=connection,
                    process_id=request.child_process_id,
                    event_name=ACTION_SUBAGENT_MESSAGE_EVENT,
                    event_id=event_id,
                    payload={
                        "message_id": request.message_id,
                        "content": request.content,
                    },
                    created_at=now_utc_iso(),
                )


def append_action_subagent_event(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    event_name: str,
    event_payload: Mapping[str, object],
) -> None:
    """Append one history row for the child whose job this worker is running."""

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            _require_running_child(connection, payload)
            append_process_event_in_connection(
                connection=connection,
                process_id=payload["process_id"],
                event_name=event_name,
                payload=dict(event_payload),
                created_at=now_utc_iso(),
            )


def deliver_action_subagent_messages(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    payload: ActionSubagentJobPayload,
    mark: bool,
) -> tuple[str, ...]:
    """The parent messages not yet in the child's conversation, marked delivered.

    Read and marked in one transaction, so every message before the marker is
    one this delivery returned, and a message that lands later is the next's.
    ``mark`` sets the marker without a message, for another row it delivers.
    """

    process_id = payload["process_id"]
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            _require_running_child(connection, payload)
            rows = connection.execute(
                "SELECT payload_json FROM process_events "
                "WHERE process_id=? AND event_name=? AND event_seq>("
                "SELECT COALESCE(MAX(event_seq),0) FROM process_events "
                "WHERE process_id=? AND event_name=?) ORDER BY event_seq",
                (
                    process_id,
                    ACTION_SUBAGENT_MESSAGE_EVENT,
                    process_id,
                    ACTION_SUBAGENT_DELIVERED_EVENT,
                ),
            ).fetchall()
            if rows or mark:
                append_process_event_in_connection(
                    connection=connection,
                    process_id=process_id,
                    event_name=ACTION_SUBAGENT_DELIVERED_EVENT,
                    payload={},
                    created_at=now_utc_iso(),
                )
    return tuple(action_subagent_message_content(_object(row[0])) for row in rows)


def load_action_subagent_events(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    process_id: str,
    event_names: Sequence[str],
) -> tuple[ActionSubagentEvent, ...]:
    """Read the child's rows of these kinds, append-only and in order."""

    placeholders = ",".join("?" * len(event_names))
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            "SELECT event_name,payload_json FROM process_events "
            f"WHERE process_id=? AND event_name IN ({placeholders}) "
            "ORDER BY event_seq",
            (process_id, *event_names),
        ).fetchall()
    return tuple(ActionSubagentEvent(str(row[0]), _object(row[1])) for row in rows)


def action_subagent_message_content(payload: dict[str, JSONValue]) -> str:
    content = payload.get("content")
    if not isinstance(content, str):
        raise MigrationError("Action subagent message event is malformed")
    return content


def _validate_request(request: ActionSubagentMessageRequest) -> None:
    if (
        request.message_id != request.message_id.strip()
        or not request.message_id
        or len(request.message_id) > ACTION_SUBAGENT_MESSAGE_ID_MAX_CODEPOINTS
    ):
        raise ActionSubagentMessageInputError("message_id is invalid")
    if (
        not request.content.strip()
        or len(request.content) > ACTION_SUBAGENT_MESSAGE_MAX_CODEPOINTS
    ):
        raise ActionSubagentMessageInputError("content is invalid")


def _require_authority(
    connection: sqlite3.Connection, request: ActionSubagentMessageRequest
) -> str:
    row = connection.execute(
        """
        SELECT child.status,
               child.parent_process_id=parent.process_id
               AND child.action_id=parent.action_id
               AND child.user_id=parent.user_id
               AND child.kind='action_subagent' AS owned_child
        FROM agent_actions AS action
        JOIN processes AS parent ON parent.action_id=action.action_id
          AND parent.user_id=action.user_id
        JOIN jobs AS job ON job.job_id=parent.current_job_id
          AND job.process_id=parent.process_id AND job.user_id=parent.user_id
        LEFT JOIN processes AS child ON child.process_id=?
        WHERE action.action_id=? AND action.user_id=? AND action.status='processing'
          AND parent.process_id=? AND parent.kind='action' AND parent.status='running'
          AND parent.current_job_id=? AND job.job_type=? AND job.status='running'
          AND job.logical_key=action.action_id
        """,
        (
            request.child_process_id,
            request.action_id,
            request.user_id,
            request.parent_process_id,
            request.parent_job_id,
            LOCAL_ACTION_JOB_TYPE,
        ),
    ).fetchone()
    if row is None:
        raise ActionSubagentMessageAuthorityError(
            "active parent trace is not available"
        )
    if row["owned_child"] != 1:
        raise ActionSubagentMessageInputError(
            "child_process_id is not owned by the active parent"
        )
    return str(row[0])


def _require_running_child(
    connection: sqlite3.Connection, payload: ActionSubagentJobPayload
) -> None:
    """Only the worker running the child's own job writes its history."""

    owner = connection.execute(
        """
        SELECT 1 FROM processes AS process
        JOIN jobs AS job ON job.job_id=process.current_job_id
        WHERE process.process_id=? AND process.user_id=? AND process.action_id=?
          AND process.kind='action_subagent' AND process.status='running'
          AND process.current_job_id=? AND job.process_id=process.process_id
          AND job.user_id=process.user_id AND job.job_type='execute_action_subagent'
          AND job.status='running'
        """,
        (
            payload["process_id"],
            payload["user_id"],
            payload["action_id"],
            payload["job_id"],
        ),
    ).fetchone()
    if owner is None:
        raise ActionSubagentMessageAuthorityError(
            "Action subagent history authority is not active"
        )


def _object(raw: object) -> dict[str, JSONValue]:
    value = json.loads(raw) if isinstance(raw, str) else None
    if not isinstance(value, dict):
        raise MigrationError("Action subagent event payload must be an object")
    return cast(dict[str, JSONValue], value)

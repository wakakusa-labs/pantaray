from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.contracts.conversation import (
    LlmConversation,
    LlmTurnAssistantItem,
    LlmTurnItem,
    LlmTurnToolResultItem,
    LlmTurnUserItem,
)
from pantaray_llm.contracts.input_block import LlmInputTextBlock
from pantaray_llm.contracts.tool_use import LlmToolCall

from ..storage.migrations import MigrationError
from ..storage.migrations.connection import configure_connection
from ..storage.transactions import immediate_transaction
from .job_types import LOCAL_ACTION_JOB_TYPE
from .process_events import append_process_event_in_connection
from .utc_timestamps import now_utc_iso

ACTION_SUBAGENT_MESSAGE_MAX_CODEPOINTS = 8_000
ACTION_SUBAGENT_MESSAGE_ID_MAX_CODEPOINTS = 128
_MESSAGE_EVENT = "action_subagent_message"
ACTION_SUBAGENT_TOOL_EVENT = "action_subagent_tool"
_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
# The heading that tells the child the item under it is feedback on its own
# last attempt rather than a message from the parent.
REPAIR_NOTICE_HEADING = "# Previous Error"
_CALL_ID_PREFIX = "subagent-call-"


@dataclass(frozen=True, slots=True)
class ActionSubagentParentMessage:
    """One message the parent sent into the running child."""

    event_seq: int
    content: str


@dataclass(frozen=True, slots=True)
class ActionSubagentToolExchange:
    """One call the child made, with the result that answered it."""

    event_seq: int
    tool_name: str
    status: Literal["completed", "error"]
    arguments: dict[str, JSONValue]
    output: JSONValue
    error_message: str | None

    @property
    def call_id(self) -> str:
        """The identity that pairs this call with its result.

        Synthesized rather than recorded: a provider only requires that a result
        names a call announced before it, and ``event_seq`` is unique within the
        child's process and survives a restart, so one exchange keeps the same
        identity on every later turn -- which is what lets the turns append.
        """

        return f"{_CALL_ID_PREFIX}{self.event_seq}"


# What one row of the child's durable transcript means. Both shapes the turn can
# be sent in are drawn from these, so neither reads ``process_events`` itself.
type ActionSubagentTranscriptEntry = (
    ActionSubagentParentMessage | ActionSubagentToolExchange
)


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
                if existing["event_name"] != _MESSAGE_EVENT or payload != {
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
                    event_name=_MESSAGE_EVENT,
                    event_id=event_id,
                    payload={
                        "message_id": request.message_id,
                        "content": request.content,
                    },
                    created_at=now_utc_iso(),
                )


def append_action_subagent_tool_transcript(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    process_id: str,
    job_id: str,
    tool_name: str,
    status: Literal["completed", "error"],
    arguments: JSONValue,
    output: JSONValue,
    error_message: str | None,
    completed_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
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
                (process_id, user_id, action_id, job_id),
            ).fetchone()
            if owner is None:
                raise ActionSubagentMessageAuthorityError(
                    "Action subagent Tool transcript authority is not active"
                )
            append_process_event_in_connection(
                connection=connection,
                process_id=process_id,
                event_name=ACTION_SUBAGENT_TOOL_EVENT,
                payload={
                    "tool_name": tool_name,
                    "status": status,
                    "arguments": arguments,
                    "output": output,
                    "error_message": error_message,
                },
                created_at=completed_at,
            )


def load_action_subagent_transcript(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    process_id: str,
) -> tuple[ActionSubagentTranscriptEntry, ...]:
    """Read the child's durable transcript, append-only and in order."""

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT event_seq,event_name,payload_json FROM process_events "
            "WHERE process_id=? AND event_name IN (?,?) ORDER BY event_seq",
            (process_id, _MESSAGE_EVENT, ACTION_SUBAGENT_TOOL_EVENT),
        ).fetchall()
    return tuple(_transcript_entry(row) for row in rows)


def build_action_subagent_conversation(
    entries: Sequence[ActionSubagentTranscriptEntry],
    *,
    assigned_task: str,
    repair_notice: str | None,
) -> LlmConversation:
    """Lay the same transcript out as provider-neutral conversation items.

    A prompt cache reads only what the previous request already sent as an exact
    prefix, so the turn the child sends has to grow by appending: the request's
    own message is the parent's context and never changes, the assigned task is
    the first item, and the rest are the rows behind it, which are final the
    moment they are appended.

    ``repair_notice`` is the retry feedback for one attempt. It goes last and is
    never recorded, so the turn after a repair appends to the request that
    preceded the notice rather than rewriting it.
    """

    items: list[LlmTurnItem] = [_text_item(assigned_task)]
    for entry in entries:
        if isinstance(entry, ActionSubagentParentMessage):
            items.append(_text_item(entry.content))
            continue
        items.append(
            LlmTurnAssistantItem(
                type="assistant",
                calls=[
                    LlmToolCall(
                        call_id=entry.call_id,
                        name=entry.tool_name,
                        arguments=entry.arguments,
                    )
                ],
            )
        )
        items.append(
            LlmTurnToolResultItem(
                type="tool_result",
                call_id=entry.call_id,
                name=entry.tool_name,
                output=_tool_result_body(entry),
            )
        )
    if repair_notice:
        items.append(_text_item(f"{REPAIR_NOTICE_HEADING}\n{repair_notice}"))
    return items


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


def _transcript_entry(row: sqlite3.Row) -> ActionSubagentTranscriptEntry:
    payload = _object(row["payload_json"])
    event_seq = int(row["event_seq"])
    if row["event_name"] == _MESSAGE_EVENT:
        content = payload.get("content")
        if isinstance(content, str):
            return ActionSubagentParentMessage(event_seq=event_seq, content=content)
    else:
        name, status, error, arguments = (
            payload.get("tool_name"),
            payload.get("status"),
            payload.get("error_message"),
            payload.get("arguments"),
        )
        if (
            isinstance(name, str)
            and status in ("completed", "error")
            and (error is None or isinstance(error, str))
            and isinstance(arguments, dict)
        ):
            return ActionSubagentToolExchange(
                event_seq=event_seq,
                tool_name=name,
                status=cast(Literal["completed", "error"], status),
                arguments=cast(dict[str, JSONValue], arguments),
                output=payload.get("output"),
                error_message=error,
            )
    raise MigrationError("Action subagent private event is malformed")


def _tool_result_body(entry: ActionSubagentToolExchange) -> JSONValue:
    """What the child reads back for one call: status, result, error."""

    body: dict[str, JSONValue] = {"status": entry.status, "result": entry.output}
    if entry.error_message:
        body["error"] = entry.error_message
    return body


def _text_item(text: str) -> LlmTurnUserItem:
    return LlmTurnUserItem(
        type="user", content=[LlmInputTextBlock(type="input_text", text=text)]
    )


def _object(raw: object) -> dict[str, JSONValue]:
    value = json.loads(raw) if isinstance(raw, str) else None
    if not isinstance(value, dict):
        raise MigrationError("Action subagent event payload must be an object")
    return cast(dict[str, JSONValue], value)

"""Local-runtime action forwarder operations."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.action_approval_projection import (
    ACTION_APPROVAL_SNAPSHOT_EVENT,
)
from pantaray_agents.local_runtime.runtime.action_malformed_stream_terminal import (
    ActionMalformedStreamTerminalRepository,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.process_events import (
    PROCESS_STATUS_PAUSED,
    read_local_process_events_after,
)
from pantaray_agents.local_runtime.runtime.screen_capture_broker import (
    SCREEN_CAPTURE_REQUESTED_EVENT,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.suggestion_state.event_names import (
    ACTION_RELAY_INTERNAL_EVENT_NAMES,
)
from pantaray_agents.local_runtime.suggestion_state.repository import (
    LocalSuggestionStateRepository,
)
from pantaray_agents.schema.action_conversation import parse_action_step_event
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    PhysicalActionRunCompletedMessage,
    ProcessPausedMessage,
    ScreenCaptureRequestedMessage,
)
from pantaray_agents.utils.metrics import record_action_job_completed
from pantaray_agents.utils.public_error import public_ws_error

from .action_relay_shared import (
    _ACTION_STREAM_PAYLOAD_INVALID_ERROR_CODE,
    _ACTION_STREAM_PAYLOAD_INVALID_ERROR_MESSAGE,
    ActionStreamPayloadValidationError,
    _ActionStreamTerminalizationResult,
    _validate_stream_end_stream_data,
)
from .base import PublicProcessEventPersistenceError

_PROCESS_PAUSED_EVENT = "process_paused"

_PAUSE_ANCHOR_STILL_HOLDS_SQL = """
SELECT EXISTS (
    SELECT 1 FROM processes
    WHERE process_id = :process_id AND status = :paused_status
) AND :cursor = (
    SELECT MAX(process_event_rowid) FROM process_events
    WHERE process_id = :process_id AND event_name = :pause_event
)
"""


def _pause_anchor_still_holds(
    *, db_path: Path, busy_timeout_ms: int, process_id: str, cursor: int
) -> bool:
    """Report whether the runtime is still stopped on exactly this pause anchor.

    A client's public cursor stops at the snapshot that precedes an anchor, so a
    reattached forwarder always reads an anchor again. Only the newest anchor of
    a process that is still paused ends the forwarder: an approved resume, and
    any pause generation after it, leave older anchors behind as replayed
    history that must be skipped.
    """

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            _PAUSE_ANCHOR_STILL_HOLDS_SQL,
            {
                "cursor": cursor,
                "pause_event": _PROCESS_PAUSED_EVENT,
                "paused_status": PROCESS_STATUS_PAUSED,
                "process_id": process_id,
            },
        ).fetchone()
    return bool(row[0])


async def _terminalize_malformed_local_action_stream(
    self,
    *,
    process_id: str,
    suggestion_id: str | None,
    action_id: str,
    command_id: str,
    malformed_event_id: str,
) -> _ActionStreamTerminalizationResult:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    result = ActionMalformedStreamTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ).finalize(
        user_id=str(self.user_id),
        suggestion_id=suggestion_id,
        action_id=str(action_id),
        process_id=str(process_id),
        command_id=str(command_id),
        malformed_event_id=str(malformed_event_id),
        completed_at=now_utc_iso(),
        failure_code=_ACTION_STREAM_PAYLOAD_INVALID_ERROR_CODE,
        failure_message_public=_ACTION_STREAM_PAYLOAD_INVALID_ERROR_MESSAGE,
    )
    return _ActionStreamTerminalizationResult(
        terminal_status=result.terminal_status,
        terminal_event_id=result.terminal_event_id,
        emit_invalid_payload_error=result.emit_invalid_payload_error,
    )


def _sync_action_process_metadata_or_release(
    self,
    *,
    process_id: str,
    suggestion_id: str | None,
    action_id: str,
    command_id: str,
    meta: dict[str, str | None],
) -> None:
    try:
        self._sync_process_metadata_to_current_session(
            process_id,
            suggestion_id=suggestion_id,
            action_id=action_id,
            command_id=command_id,
            kind="action",
        )
    except ValueError:
        self._release_action_process(process_id)
        self._process_metadata[process_id] = dict(meta)
        raise


async def _forward_action_events_from_local_runtime(
    self,
    *,
    process_id: str,
    logical_run_id: str,
    suggestion_id: str | None,
    action_id: str,
    command_id: str,
    start_after_cursor: int = 0,
) -> None:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    last_cursor = int(start_after_cursor)
    poll_interval_seconds = 0.1
    meta = {
        "process_id": process_id,
        "suggestion_id": suggestion_id,
        "action_id": action_id,
        "command_id": command_id,
        "kind": "action",
    }
    while True:
        if getattr(self, "_is_closed", False):
            return
        if process_id not in getattr(self, "_action_processes", set()):
            return
        events = read_local_process_events_after(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            process_id=process_id,
            after_cursor=last_cursor,
            limit=50,
        )
        if not events:
            await asyncio.sleep(poll_interval_seconds)
            continue
        for event in events:
            event_name = str(event.event_type or "unknown")
            if event_name == "action_step":
                action_step = parse_action_step_event(event.payload)
                sent = await self._send(
                    OutboundEvent.ACTION_STEP.value,
                    action_step,
                    process_id=process_id,
                    meta={**meta, "logical_run_id": logical_run_id},
                    event_id=str(event.event_id),
                    store_in_session_store=False,
                    persist_public_event=False,
                )
                if not sent:
                    return
                last_cursor = event.cursor
                continue
            if event_name in ACTION_RELAY_INTERNAL_EVENT_NAMES:
                last_cursor = event.cursor
                continue
            if event_name == "process_started":
                started_accepted_at = (
                    str(event.payload.get("accepted_at") or "").strip() or None
                )
                started_at = str(event.payload.get("started_at") or "").strip() or None
                persisted_sequence_raw = event.payload.get("persisted_sequence")
                persisted_sequence = (
                    int(persisted_sequence_raw)
                    if isinstance(persisted_sequence_raw, int)
                    else None
                )
                if started_accepted_at is None or started_at is None:
                    last_cursor = event.cursor
                    continue
                sent = await self._replay_process_started_action(
                    process_id=process_id,
                    suggestion_id=suggestion_id,
                    action_id=action_id,
                    command_id=command_id,
                    accepted_at=started_accepted_at,
                    started_at=started_at,
                    event_id=str(event.event_id),
                    store_in_session_store=False,
                    persisted_sequence_override=persisted_sequence,
                )
                if not sent:
                    return
                last_cursor = event.cursor
                continue
            if event_name == SCREEN_CAPTURE_REQUESTED_EVENT:
                # A control message for the desktop client, not conversation
                # history: it is never persisted and never replayed, because a
                # capture request that arrives late has nothing left to answer.
                sent = await self._send(
                    OutboundEvent.SCREEN_CAPTURE_REQUESTED.value,
                    ScreenCaptureRequestedMessage(
                        kind="action",
                        process_id=str(event.payload["process_id"]),
                        action_id=str(event.payload["action_id"]),
                        tool_request_id=str(event.payload["tool_request_id"]),
                        capture_request_id=str(event.payload["capture_request_id"]),
                        app_name=str(event.payload["app_name"]),
                    ),
                    process_id=process_id,
                    meta=dict(meta),
                    event_id=str(event.event_id),
                    store_in_session_store=False,
                    persist_public_event=False,
                )
                if not sent:
                    return
                last_cursor = event.cursor
                continue
            if event_name == ACTION_APPROVAL_SNAPSHOT_EVENT:
                # The snapshot carries the Action's whole waiting set, root and
                # subagent alike, so it is published even when nothing waits any
                # more and it never ends this forwarder.
                paused_message = ProcessPausedMessage.model_validate(
                    {
                        "kind": "action",
                        "process_id": process_id,
                        "suggestion_id": suggestion_id,
                        "action_id": action_id,
                        "command_id": command_id,
                        "status": "processing",
                        "reason": "approval_pending",
                        "completed_at": event.payload.get("completed_at"),
                        "approval_blockers": event.payload.get("approval_blockers"),
                    }
                )
                persisted_sequence = None
                if suggestion_id is not None:
                    repository = LocalSuggestionStateRepository(
                        db_path=db_path,
                        busy_timeout_ms=busy_timeout_ms,
                    )
                    pause_result = await repository.append_action_pause_if_processing(
                        event_id=str(event.event_id),
                        suggestion_id=suggestion_id,
                        user_id=str(self.user_id),
                        action_id=action_id,
                        payload={
                            "data": paused_message.model_dump(mode="json"),
                            "meta": dict(meta),
                        },
                    )
                    if pause_result.error:
                        raise PublicProcessEventPersistenceError(pause_result.error)
                    if pause_result.data is None:
                        last_cursor = event.cursor
                        continue
                    persisted_sequence = pause_result.data.sequence
                sent = await self._send(
                    OutboundEvent.PROCESS_PAUSED.value,
                    paused_message,
                    process_id=process_id,
                    meta=dict(meta),
                    event_id=str(event.event_id),
                    store_in_session_store=False,
                    persist_public_event=False,
                    persisted_sequence_override=persisted_sequence,
                )
                if not sent:
                    return
                last_cursor = event.cursor
                continue
            if event_name == _PROCESS_PAUSED_EVENT:
                # Only the root's own physical pause reaches this stream, and the
                # anchor the runtime currently rests on is the sole event that
                # ends this forwarder. The waiting set is published by the
                # snapshot above, and superseded anchors are skipped history.
                if not _pause_anchor_still_holds(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    process_id=process_id,
                    cursor=event.cursor,
                ):
                    last_cursor = event.cursor
                    continue
                _sync_action_process_metadata_or_release(
                    self,
                    process_id=process_id,
                    suggestion_id=suggestion_id,
                    action_id=action_id,
                    command_id=command_id,
                    meta=meta,
                )
                self._release_action_process(process_id)
                self._process_metadata[process_id] = dict(meta)
                return
            if event_name == "error":
                # Runtime diagnostics are persisted in the local process stream.
                # The canonical process_completed event is the sole public terminal
                # event, so an ordinary runtime failure is not published twice.
                last_cursor = event.cursor
                continue
            if event_name == "stream_end":
                try:
                    terminal_data = _validate_stream_end_stream_data(event.payload)
                except ActionStreamPayloadValidationError:
                    terminalization = await _terminalize_malformed_local_action_stream(
                        self,
                        process_id=process_id,
                        suggestion_id=suggestion_id,
                        action_id=action_id,
                        command_id=command_id,
                        malformed_event_id=str(event.event_id),
                    )
                    _sync_action_process_metadata_or_release(
                        self,
                        process_id=process_id,
                        suggestion_id=suggestion_id,
                        action_id=action_id,
                        command_id=command_id,
                        meta=meta,
                    )
                    if (
                        terminalization.emit_invalid_payload_error
                        and suggestion_id is not None
                    ):
                        await self._send_action_error(
                            public_ws_error(
                                error_code=_ACTION_STREAM_PAYLOAD_INVALID_ERROR_CODE,
                                request_id=getattr(self, "session_id", None),
                                error_type=ErrorType.INTERNAL_ERROR,
                                severity=ErrorSeverity.ERROR,
                                error_message=_ACTION_STREAM_PAYLOAD_INVALID_ERROR_MESSAGE,
                            ),
                            suggestion_id=suggestion_id,
                            command_id=command_id,
                            process_id=process_id,
                            action_id=action_id,
                            action_stage="running_failed",
                            event_id=terminalization.error_event_id,
                            store_in_session_store=False,
                            persist_public_event=False,
                        )
                    sent = await self._emit_process_completed_action(
                        process_id=process_id,
                        suggestion_id=suggestion_id,
                        action_id=action_id,
                        command_id=command_id,
                        status=terminalization.terminal_status,
                        event_id=terminalization.terminal_event_id,
                        store_in_session_store=True,
                    )
                    if not sent:
                        return
                    self._record_action_terminal_completion_once(
                        process_id=process_id,
                        status=terminalization.terminal_status,
                    )
                    self._release_action_process(process_id)
                    return
                _sync_action_process_metadata_or_release(
                    self,
                    process_id=process_id,
                    suggestion_id=suggestion_id,
                    action_id=action_id,
                    command_id=command_id,
                    meta=meta,
                )
                persisted_sequence = terminal_data.get("persisted_sequence")
                if persisted_sequence is None:
                    if terminal_data.get("physical_run_only"):
                        sent = await self._send(
                            OutboundEvent.PROCESS_COMPLETED.value,
                            PhysicalActionRunCompletedMessage(
                                kind="action",
                                physical_run_only=True,
                                process_id=process_id,
                                status="canceled",
                            ),
                            process_id=process_id,
                            meta={"kind": "action"},
                            event_id=str(event.event_id),
                            store_in_session_store=True,
                            persist_public_event=False,
                        )
                        if sent:
                            record_action_job_completed(status="canceled")
                        terminal_status = terminal_data["status"]
                    else:
                        terminal_status = terminal_data["status"]
                        sent = await self._emit_process_completed_action(
                            process_id=process_id,
                            suggestion_id=suggestion_id,
                            action_id=action_id,
                            command_id=command_id,
                            status=terminal_status,
                            event_id=str(event.event_id),
                            store_in_session_store=True,
                        )
                    if not sent:
                        return
                else:
                    terminal_status = terminal_data["status"]
                    sent = await self._emit_process_completed_action(
                        process_id=process_id,
                        suggestion_id=suggestion_id,
                        action_id=action_id,
                        command_id=command_id,
                        status=terminal_status,
                        event_id=str(event.event_id),
                        persisted_sequence_override=persisted_sequence,
                    )
                    if not sent:
                        return
                self._record_action_terminal_completion_once(
                    process_id=process_id,
                    status=terminal_status,
                )
                self._release_action_process(process_id)
                return
            last_cursor = event.cursor

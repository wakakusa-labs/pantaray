"""WS handler process control responsibilities."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import HTTPException

from pantaray_agents.action_status import (
    parse_stored_suggestion_user_reaction,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.routers.action_cancel_service import execute_action_cancel
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket import AckEventMessage
from pantaray_agents.utils.metrics import (
    record_action_event_acked,
    record_suggestion_event_acked,
)
from pantaray_agents.utils.public_error import public_ws_error

from .action_relay_authority import load_action_relay_authority
from .action_relay_shared import ACTION_EVENT_CURSOR_START_ID
from .error_meta import build_protocol_error_meta, build_suggestion_error_meta
from .handler_shared import SuggestionStatusPersistenceError

logger = logging.getLogger(__name__)


class WSHandlerProcessControlMixin:
    """Action control / dismiss / ack / close responsibilities."""

    async def handle_stop_process(self, process_id: str) -> None:
        sess = self.session_store.get(self.session_id)
        proc = sess.processes.get(process_id) if sess else None
        bound_meta = self._process_metadata.get(process_id, {})
        if bound_meta.get("kind") == "suggestion":
            self.cancel_process(process_id)
            return
        stored_action_id = proc.action_id if proc and proc.kind == "action" else None
        bound_action_id = (
            bound_meta.get("action_id") if bound_meta.get("kind") == "action" else None
        )
        action_id = stored_action_id or bound_action_id
        if isinstance(action_id, str):
            try:
                terminal = load_action_relay_authority(
                    user_id=str(self.user_id),
                    action_id=action_id,
                    process_id=process_id,
                )
                if (
                    terminal.terminal_cursor is not None
                    and proc is not None
                    and proc.completed_at is not None
                ):
                    return
                if terminal.terminal_cursor is None:
                    await execute_action_cancel(
                        user_id=terminal.user_id,
                        action_id=terminal.action_id,
                        reason="user_stop",
                        expected_process_id=terminal.process_id,
                    )
                    terminal = load_action_relay_authority(
                        user_id=terminal.user_id,
                        action_id=terminal.action_id,
                        process_id=terminal.process_id,
                    )
                    current_session = self.session_store.get(self.session_id)
                    current_process = (
                        current_session.processes.get(process_id)
                        if current_session
                        else None
                    )
                    if process_id in self._action_completed:
                        return
                    if current_process and current_process.completed_at is not None:
                        return
                if terminal.terminal_cursor is None:
                    raise MigrationError("Action terminal cursor is unavailable")
                self._bind_action_process(
                    terminal.process_id,
                    terminal.suggestion_id,
                    terminal.action_id,
                )
                self._start_action_event_forwarder(
                    process_id=terminal.process_id,
                    logical_run_id=terminal.root_process_id,
                    suggestion_id=terminal.suggestion_id,
                    action_id=terminal.action_id,
                    command_id=terminal.command_id,
                    start_event_id=ACTION_EVENT_CURSOR_START_ID,
                    start_after_cursor=terminal.terminal_cursor - 1,
                )
            except (
                HTTPException,
                MigrationError,
                OSError,
                RuntimeError,
                sqlite3.Error,
            ) as exc:
                logger.warning(
                    "Bound Action stop was rejected: process_id=%s error_type=%s",
                    process_id,
                    type(exc).__name__,
                )
            return

        self.cancel_process(process_id)

    def cancel_process(self, process_id: str) -> None:
        self._task_supervisor.cancel(process_id)
        meta = self._process_metadata.get(process_id)
        if (
            meta and meta.get("kind") == "action"
        ) or process_id in self._action_processes:
            return
        self._release_suggestion_process(process_id)

    async def _send_suggestion_persistence_error(
        self,
        *,
        suggestion_id: str,
        failure_kind: str,
        exc: SuggestionStatusPersistenceError,
    ) -> None:
        logger.warning(
            "Suggestion persistence failed: suggestion_id=%s failure_kind=%s error=%s",
            suggestion_id,
            failure_kind,
            exc,
            exc_info=True,
        )
        await self.send_error(
            public_ws_error(
                error_code="WS_DEPENDENCY_UNAVAILABLE",
                request_id=self.session_id,
                error_type=ErrorType.INTERNAL_ERROR,
                severity=ErrorSeverity.ERROR,
                error_message="Failed to persist suggestion state.",
                extra_error_details={"dependency": "local_runtime_db"},
            ),
            meta=build_suggestion_error_meta(
                suggestion_id=str(suggestion_id),
                stage="persist_status_failed",
                error_code="WS_DEPENDENCY_UNAVAILABLE",
                failure_kind=str(failure_kind),
            ),
        )

    async def handle_dismiss(self, suggestion_id: str) -> None:
        if not await self._is_suggestion_id_accessible(suggestion_id):
            await self.send_error(
                public_ws_error(
                    error_code="WS_SUGGESTION_NOT_FOUND",
                    request_id=self.session_id,
                    error_type=ErrorType.VALIDATION_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="suggestion_id is not accessible for current user",
                ),
                meta=build_protocol_error_meta(
                    stage="suggestion_access_denied",
                    error_code="WS_SUGGESTION_NOT_FOUND",
                ),
            )
            return
        repo = await self._get_suggestion_repository()
        if repo is None:
            await self.send_error(
                public_ws_error(
                    error_code="WS_DEPENDENCY_UNAVAILABLE",
                    request_id=self.session_id,
                    error_type=ErrorType.INTERNAL_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="Failed to load suggestion state.",
                    extra_error_details={"dependency": "local_runtime_db"},
                ),
                meta=build_suggestion_error_meta(
                    suggestion_id=str(suggestion_id),
                    stage="load_state_failed",
                    error_code="WS_DEPENDENCY_UNAVAILABLE",
                ),
            )
            return
        state_result = await repo.get_suggestion_state(
            user_id=str(self.user_id), suggestion_id=str(suggestion_id)
        )
        row = state_result.data if isinstance(state_result.data, dict) else None
        interaction_contract = (
            str(row.get("interaction_contract") or "").strip().lower() if row else ""
        )
        current_reaction = (
            parse_stored_suggestion_user_reaction(row.get("user_reaction"))
            if row
            else None
        )
        if interaction_contract != "action_offer" or current_reaction:
            await self.send_error(
                public_ws_error(
                    error_code="WS_ACTION_NOT_ALLOWED",
                    request_id=self.session_id,
                    error_type=ErrorType.VALIDATION_ERROR,
                    severity=ErrorSeverity.ERROR,
                    error_message="dismiss_suggestion is not allowed for current suggestion state",
                ),
                meta=build_suggestion_error_meta(
                    suggestion_id=str(suggestion_id),
                    stage="dismiss_not_allowed",
                    error_code="WS_ACTION_NOT_ALLOWED",
                ),
            )
            return
        try:
            await super().handle_dismiss(suggestion_id)
        except SuggestionStatusPersistenceError as exc:
            await self._send_suggestion_persistence_error(
                suggestion_id=suggestion_id,
                failure_kind="persist_dismiss_reaction",
                exc=exc,
            )

    async def close(self) -> None:
        self._is_closed = True
        for process_id in list(self._action_processes):
            self._release_action_process(process_id)
        for pid, meta in list(self._process_metadata.items()):
            if str(meta.get("kind") or "") == "suggestion":
                try:
                    self._release_suggestion_process(pid)
                except Exception:
                    pass
        self.stop_suggestion_relay()
        await self._task_supervisor.close()

    async def handle_ack(self, message: AckEventMessage) -> None:
        if str(getattr(message, "session_id", "")) != str(self.session_id):
            return

        meta = self.session_store.ack_event(self.session_id, message.event_id)
        if not meta:
            return
        process_id = meta.get("process_id")
        if process_id and str(process_id) != str(message.process_id):
            return
        event_name = str(meta.get("event") or "unknown")
        kind = meta.get("kind")
        if kind == "action" or (process_id and process_id in self._action_processes):
            record_action_event_acked(event_name)
        elif kind == "suggestion" or (
            process_id
            and self._process_metadata.get(process_id, {}).get("kind") == "suggestion"
        ):
            record_suggestion_event_acked(event_name)
        if event_name == OutboundEvent.PROCESS_COMPLETED.value and process_id:
            self._discard_completed_process_metadata(str(process_id))

    def _discard_completed_process_metadata(self, process_id: str) -> None:
        self.session_store.discard_completed_process(self.session_id, process_id)
        self._action_processes.discard(process_id)
        self._process_metadata.pop(process_id, None)

"""WS handler process control responsibilities."""

from __future__ import annotations

import logging

from pantaray_agents.action_status import (
    parse_stored_suggestion_user_reaction,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket import AckEventMessage
from pantaray_agents.utils.metrics import (
    record_action_event_acked,
    record_suggestion_event_acked,
)
from pantaray_agents.utils.public_error import public_ws_error

from .error_meta import build_suggestion_error_meta
from .handler_shared import SuggestionStatusPersistenceError

logger = logging.getLogger(__name__)


class WSHandlerProcessControlMixin:
    """Dismiss / ack / close responsibilities."""

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

    async def handle_dismiss(self, suggestion_id: str, *, reason: str | None) -> None:
        """Record a dismissal, with the user's reason if they wrote one.

        Only an unanswered offer can be dismissed, so a repeated dismissal is
        refused and the first one, with its reason, stays. The router has
        already checked that the suggestion belongs to this user.
        """

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
        rejected_at = now_utc_iso()
        try:
            stored = await self._persist_suggestion_status(
                suggestion_id,
                user_reaction="rejected",
                action_status="idle",
                rejected_at=rejected_at,
                accepted_at=None,
                dismiss_reason=reason,
            )
        except SuggestionStatusPersistenceError as exc:
            await self._send_suggestion_persistence_error(
                suggestion_id=suggestion_id,
                failure_kind="persist_dismiss_reaction",
                exc=exc,
            )
            return
        committed_at = (
            str(stored.get("rejected_at") or rejected_at)
            if isinstance(stored, dict)
            else rejected_at
        )
        await self.emit_suggestion_reaction_committed(
            suggestion_id=suggestion_id,
            reaction="rejected",
            committed_at=committed_at,
            reason=reason,
            meta={"suggestion_id": suggestion_id, "kind": "suggestion"},
        )

    async def handle_reject(self, suggestion_id: str) -> None:
        """後方互換: reject は dismiss と同義として扱う。"""
        await self.handle_dismiss(suggestion_id, reason=None)

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
        self.stop_chat_relay()
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

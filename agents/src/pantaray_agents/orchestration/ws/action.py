"""WebSocket adapter for accepting a Suggestion."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from pantaray_agents.action_status import is_action_terminal_status
from pantaray_agents.local_runtime.runtime.action_messages import (
    StartedActionMessageResult,
)
from pantaray_agents.local_runtime.runtime.suggestion_acceptance import (
    ActionLanguage,
    SuggestionAcceptanceRejected,
    SuggestionAcceptanceRejection,
    accept_suggestion,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.orchestration.ws.action_relay import (
    ACTION_EVENT_CURSOR_START_ID,
    ActionRelayMixin,
)
from pantaray_agents.orchestration.ws.base import BaseWSHandler
from pantaray_agents.orchestration.ws.error_meta import build_session_error_meta
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.websocket import ExecuteActionMessage
from pantaray_agents.utils.public_error import ErrorDetails, public_ws_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ErrorResponse:
    error_code: str
    error_type: ErrorType
    error_message: str
    failure_kind: str | None
    persist_public_event: bool
    extra_error_details: ErrorDetails | None = None


_NOT_ALLOWED_MESSAGE: Final = (
    "execute_action is not allowed for current suggestion state"
)
_REJECTION_RESPONSES: Final[dict[SuggestionAcceptanceRejection, _ErrorResponse]] = {
    "suggestion_not_found": _ErrorResponse(
        error_code="WS_SUGGESTION_NOT_FOUND",
        error_type=ErrorType.VALIDATION_ERROR,
        error_message="suggestion_id is not accessible for current user",
        failure_kind=None,
        persist_public_event=True,
    ),
    "state_unavailable": _ErrorResponse(
        error_code="WS_DEPENDENCY_UNAVAILABLE",
        error_type=ErrorType.INTERNAL_ERROR,
        error_message="Failed to load suggestion state (fail-closed).",
        failure_kind="load_suggestion_state",
        persist_public_event=True,
        extra_error_details={"dependency": "local_state"},
    ),
    "not_allowed": _ErrorResponse(
        error_code="WS_ACTION_NOT_ALLOWED",
        error_type=ErrorType.VALIDATION_ERROR,
        error_message=_NOT_ALLOWED_MESSAGE,
        failure_kind=None,
        persist_public_event=True,
    ),
    "already_processing": _ErrorResponse(
        error_code="WS_ACTION_ALREADY_PROCESSING",
        error_type=ErrorType.VALIDATION_ERROR,
        error_message="action is already processing",
        failure_kind=None,
        persist_public_event=True,
    ),
    "attachment_unavailable": _ErrorResponse(
        error_code="WS_ACTION_ATTACHMENT_UNAVAILABLE",
        error_type=ErrorType.INTERNAL_ERROR,
        error_message="An attached file is no longer available.",
        failure_kind="attachment_unavailable",
        persist_public_event=False,
    ),
    "creation_conflict": _ErrorResponse(
        error_code="WS_ACTION_NOT_ALLOWED",
        error_type=ErrorType.INTERNAL_ERROR,
        error_message=_NOT_ALLOWED_MESSAGE,
        failure_kind="action_state_guard",
        persist_public_event=False,
    ),
}
_CREATION_FAILED_RESPONSE: Final = _ErrorResponse(
    error_code="WS_DEPENDENCY_UNAVAILABLE",
    error_type=ErrorType.INTERNAL_ERROR,
    error_message="Failed to persist Action.",
    failure_kind="submit_action_message",
    persist_public_event=False,
)


class ActionFlowMixin(ActionRelayMixin):
    """Turn a WS ``execute_action`` into one Suggestion acceptance and its relay."""

    async def execute_action(self, payload: ExecuteActionMessage) -> None:
        command_id = str(payload.command_id)
        suggestion_id = payload.suggestion_id
        try:
            outcome = await accept_suggestion(
                user_id=str(self.user_id),
                suggestion_id=suggestion_id,
                command_id=command_id,
                approval_mode=payload.approval_mode,
                language=self._resolve_action_language(payload.language),
                supplement=payload.supplement,
                supplement_project_refs=payload.supplement_project_refs,
                images=payload.images,
                files=payload.files,
            )
        except Exception:
            logger.exception(
                "Failed to create Action: suggestion_id=%s command_id=%s",
                suggestion_id,
                command_id,
            )
            await self._send_rejection(
                _CREATION_FAILED_RESPONSE,
                suggestion_id=suggestion_id,
                command_id=command_id,
            )
            return
        if isinstance(outcome, SuggestionAcceptanceRejected):
            await self._send_rejection(
                _REJECTION_RESPONSES[outcome.reason],
                suggestion_id=suggestion_id,
                command_id=command_id,
            )
            return

        result = outcome.action
        if result.disposition == "not_executed":
            return
        if (
            isinstance(result, StartedActionMessageResult)
            and not result.inserted
            and is_action_terminal_status(result.action_status)
        ):
            terminal_status = result.action_status
            repo = await self._get_action_state_repository()
            terminal_row = None
            if repo is not None:
                try:
                    terminal_state = await repo.get_suggestion_state(
                        user_id=str(self.user_id), suggestion_id=suggestion_id
                    )
                except (MigrationError, OSError, sqlite3.Error):
                    logger.exception(
                        "Terminal projection read failed: %s", result.action_id
                    )
                else:
                    if not terminal_state.error:
                        terminal_row = terminal_state.data
            identity_matches = (
                isinstance(terminal_row, Mapping)
                and _optional_text(terminal_row.get("action_id")) == result.action_id
                and _optional_text(terminal_row.get("action_process_id"))
                == result.process_id
                and _optional_text(terminal_row.get("action_status")) == terminal_status
            )
            sent = False
            if identity_matches:
                sent = await self._emit_process_completed_action(
                    process_id=result.process_id,
                    suggestion_id=suggestion_id,
                    action_id=result.action_id,
                    command_id=command_id,
                    status=terminal_status,
                )
            if not sent:
                await self._send_action_error(
                    public_ws_error(
                        error_code="WS_DEPENDENCY_UNAVAILABLE",
                        request_id=self.session_id,
                        error_type=ErrorType.INTERNAL_ERROR,
                        severity=ErrorSeverity.ERROR,
                        error_message="Terminal Action could not be replayed.",
                    ),
                    suggestion_id=suggestion_id,
                    command_id=command_id,
                    process_id=result.process_id,
                    action_id=result.action_id,
                    action_stage="persist_final_state_failed",
                    failure_kind=(
                        "replay_process_completed_send_failed"
                        if identity_matches
                        else "replay_process_completed_identity_mismatch"
                    ),
                    persist_public_event=False,
                )
                return
            self._record_action_terminal_completion_once(
                process_id=result.process_id,
                status=terminal_status,
            )
            return
        await self._replay_action_requested(
            suggestion_id=suggestion_id,
            command_id=command_id,
            accepted_at=outcome.accepted_at,
        )
        if result.disposition != "started":
            return
        if result.process_id in self._action_processes:
            return
        await self._attach_created_action(
            result=result,
            suggestion_id=suggestion_id,
            command_id=command_id,
        )

    def _resolve_action_language(self, requested: str | None) -> ActionLanguage | None:
        if requested == "en":
            self._ui_language = requested  # type: ignore[attr-defined]
            return "en"
        if requested == "ja":
            self._ui_language = requested  # type: ignore[attr-defined]
            return "ja"
        current = getattr(self, "_ui_language", None)
        if current == "en":
            return "en"
        if current == "ja":
            return "ja"
        return None

    async def _attach_created_action(
        self,
        *,
        result: StartedActionMessageResult,
        suggestion_id: str,
        command_id: str,
    ) -> None:
        try:
            self.session_store.ensure_process(self.session_id, result.process_id)
        except Exception:
            await self._release_action_start_resources(process_id=result.process_id)
            await self._send_live_attach_error(
                error_code="WS_PROCESS_LIMIT_EXCEEDED",
            )
            return

        try:
            await self._attach_action_process_to_current_session(
                process_id=result.process_id,
                logical_run_id=result.process_id,
                suggestion_id=suggestion_id,
                action_id=result.action_id,
                command_id=command_id,
                start_event_id=ACTION_EVENT_CURSOR_START_ID,
            )
        except Exception:
            await self._release_action_start_resources(process_id=result.process_id)
            await self._send_live_attach_error(
                error_code="ACTION_RELAY_ATTACH_FAILED",
            )

    async def _send_rejection(
        self,
        response: _ErrorResponse,
        *,
        suggestion_id: str,
        command_id: str,
    ) -> None:
        await self._send_action_error(
            public_ws_error(
                error_code=response.error_code,
                request_id=getattr(self, "session_id", None),
                error_type=response.error_type,
                severity=ErrorSeverity.ERROR,
                error_message=response.error_message,
                extra_error_details=response.extra_error_details,
            ),
            suggestion_id=suggestion_id,
            command_id=command_id,
            action_stage="preflight_rejected",
            failure_kind=response.failure_kind,
            persist_public_event=response.persist_public_event,
        )

    async def _send_live_attach_error(
        self,
        *,
        error_code: str,
    ) -> None:
        await cast(BaseWSHandler, self).send_error(
            public_ws_error(
                error_code=error_code,
                request_id=getattr(self, "session_id", None),
                error_type=ErrorType.INTERNAL_ERROR,
                severity=ErrorSeverity.ERROR,
                error_message=(
                    "Action continues in the background, but live progress "
                    "could not be attached."
                ),
            ),
            meta=build_session_error_meta(
                stage="action_live_attach_failed",
                error_code=error_code,
            ),
            persist_public_event=False,
        )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None

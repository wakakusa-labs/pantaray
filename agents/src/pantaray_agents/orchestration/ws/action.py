"""Suggestion approval adapter for the canonical Action creation function."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import ValidationError

from pantaray_agents.action_status import is_action_terminal_status
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ActionMessageConflictError,
    NewActionTarget,
    StartedActionMessageResult,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.bootstrap import is_local_runtime_enabled
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.orchestration.ws.action_relay import (
    ACTION_EVENT_CURSOR_START_ID,
    ActionRelayMixin,
)
from pantaray_agents.orchestration.ws.base import BaseWSHandler
from pantaray_agents.orchestration.ws.error_meta import build_session_error_meta
from pantaray_agents.schema.agent.action import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message import (
    ActionProjectRef,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.base import ErrorSeverity, ErrorType
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.websocket import ExecuteActionMessage
from pantaray_agents.security.storage_paths import validate_image_storage_path
from pantaray_agents.utils.public_error import public_ws_error

logger = logging.getLogger(__name__)

_LOCAL_RUNTIME_REQUIRED_ERROR_CODE = "LOCAL_RUNTIME_REQUIRED"
type ActionLanguage = Literal["en", "ja"]


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _target_metadata(row: Mapping[str, object]) -> tuple[str | None, str | None]:
    target_context = row.get("target_context_json")
    if not isinstance(target_context, Mapping):
        return None, None
    return (
        _optional_text(target_context.get("organization_name")),
        _optional_text(target_context.get("project_name")),
    )


def _build_suggestion_action_command(
    *,
    user_id: str,
    suggestion_id: str,
    command_id: str,
    approved_at: str,
    language: ActionLanguage | None,
    supplement: str | None,
    supplement_project_refs: tuple[ActionProjectRef, ...],
    approval_mode: ApprovalMode,
    images: tuple[ImageInput, ...],
    files: tuple[FileAttachmentInput, ...],
    suggestion_row: Mapping[str, object],
) -> SubmitActionMessageCommand:
    content = _optional_text(suggestion_row.get("answer"))
    if content is None:
        raise ActionMessageConflictError("Suggestion answer is empty")
    organization_name, project_name = _target_metadata(suggestion_row)
    for image in images:
        try:
            validate_image_storage_path(
                user_id=user_id, storage_path=image.storage_path
            )
        except ValueError as exc:
            raise ActionMessageConflictError(
                "Suggestion image reference is invalid"
            ) from exc
    try:
        return SubmitActionMessageCommand(
            user_id=user_id,
            target=NewActionTarget(
                suggestion_id=suggestion_id, approval_mode=approval_mode
            ),
            message=ActionUserMessageInput(
                message_id=command_id,
                content=content,
                language=language,
                supplement=supplement,
                supplement_project_refs=supplement_project_refs,
                images=images,
                files=files,
                suggestion_approval=SuggestionApprovalInput(
                    suggestion_id=suggestion_id,
                    approved_at=approved_at,
                    summary=_optional_text(suggestion_row.get("suggestion_summary")),
                    organization_name=organization_name,
                    project_name=project_name,
                ),
            ),
        )
    except ValidationError as exc:
        raise ActionMessageConflictError(
            "Suggestion cannot be submitted as an Action message"
        ) from exc


class ActionFlowMixin(ActionRelayMixin):
    """Adapt Suggestion approval to the canonical Action message function."""

    async def execute_action(self, payload: ExecuteActionMessage) -> None:
        command_id = str(payload.command_id)
        suggestion_id = str(payload.suggestion_id)
        if not is_local_runtime_enabled():
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code=_LOCAL_RUNTIME_REQUIRED_ERROR_CODE,
                error_message="Local runtime must be enabled for action execution.",
                failure_kind="local_runtime_disabled",
            )
            return

        repo = await self._get_action_state_repository()
        if repo is None:
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code="WS_DEPENDENCY_UNAVAILABLE",
                error_message="Suggestion repository is unavailable.",
                failure_kind="load_suggestion_state",
            )
            return
        try:
            state_result = await repo.get_suggestion_state(
                user_id=str(self.user_id), suggestion_id=suggestion_id
            )
        except (MigrationError, OSError, sqlite3.Error):
            logger.exception("Suggestion read failed: %s", suggestion_id)
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code="WS_DEPENDENCY_UNAVAILABLE",
                error_message="Failed to load suggestion state.",
                failure_kind="load_suggestion_state",
            )
            return
        suggestion_row = state_result.data if not state_result.error else None
        if not isinstance(suggestion_row, Mapping):
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code="WS_DEPENDENCY_UNAVAILABLE",
                error_message="Failed to load suggestion state.",
                failure_kind="load_suggestion_state",
            )
            return

        approved_at = _optional_text(suggestion_row.get("accepted_at")) or now_utc_iso()
        language = self._resolve_action_language(payload.language)
        try:
            result = submit_action_message(
                _build_suggestion_action_command(
                    user_id=str(self.user_id),
                    suggestion_id=suggestion_id,
                    command_id=command_id,
                    approved_at=approved_at,
                    language=language,
                    supplement=_optional_text(payload.supplement),
                    supplement_project_refs=payload.supplement_project_refs,
                    approval_mode=payload.approval_mode,
                    images=payload.images,
                    files=payload.files,
                    suggestion_row=suggestion_row,
                )
            )
        except ActionFileAttachmentUnavailableError:
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code="WS_ACTION_ATTACHMENT_UNAVAILABLE",
                error_message="An attached file is no longer available.",
                failure_kind="attachment_unavailable",
            )
            return
        except ActionMessageConflictError:
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code="WS_ACTION_NOT_ALLOWED",
                error_message="execute_action is not allowed for current suggestion state",
                failure_kind="action_state_guard",
            )
            return
        except Exception:
            logger.exception(
                "Failed to create Action: suggestion_id=%s command_id=%s",
                suggestion_id,
                command_id,
            )
            await self._send_preflight_error(
                suggestion_id=suggestion_id,
                command_id=command_id,
                error_code="WS_DEPENDENCY_UNAVAILABLE",
                error_message="Failed to persist Action.",
                failure_kind="submit_action_message",
            )
            return

        if result.disposition == "not_executed":
            return
        if (
            isinstance(result, StartedActionMessageResult)
            and not result.inserted
            and is_action_terminal_status(result.action_status)
        ):
            terminal_status = result.action_status
            try:
                terminal_state = await repo.get_suggestion_state(
                    user_id=str(self.user_id), suggestion_id=suggestion_id
                )
            except (MigrationError, OSError, sqlite3.Error):
                logger.exception(
                    "Terminal projection read failed: %s", result.action_id
                )
                terminal_row = None
            else:
                terminal_row = terminal_state.data if not terminal_state.error else None
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
            accepted_at=approved_at,
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

    async def _send_preflight_error(
        self,
        *,
        suggestion_id: str,
        command_id: str,
        error_code: str,
        error_message: str,
        failure_kind: str,
    ) -> None:
        await self._send_action_error(
            public_ws_error(
                error_code=error_code,
                request_id=getattr(self, "session_id", None),
                error_type=ErrorType.INTERNAL_ERROR,
                severity=ErrorSeverity.ERROR,
                error_message=error_message,
            ),
            suggestion_id=suggestion_id,
            command_id=command_id,
            action_stage="preflight_rejected",
            failure_kind=failure_kind,
            persist_public_event=False,
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

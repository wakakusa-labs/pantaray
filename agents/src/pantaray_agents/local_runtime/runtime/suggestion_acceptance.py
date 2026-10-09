"""Accept one Suggestion: check its state, record the approval, start its Action."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from pantaray_agents.action_status import ACTION_STATUS_PROCESSING
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.suggestion_state.repository import (
    LocalSuggestionStateRepository,
)
from pantaray_agents.local_runtime.tooling.models import ApprovalMode
from pantaray_agents.schema.agent.action import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)
from pantaray_agents.schema.agent.action_message import (
    ActionProjectRef,
    ChatHandoffInput,
    FileAttachmentInput,
)
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.security.storage_paths import validate_image_storage_path

from .action_file_attachments import ActionFileAttachmentUnavailableError
from .action_message_models import (
    ActionMessageConflictError,
    NewActionTarget,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
)
from .action_messages import submit_action_message
from .runtime_env import read_local_runtime_db_config
from .utc_timestamps import now_utc_iso

logger = logging.getLogger(__name__)

type ActionLanguage = Literal["en", "ja"]
type SuggestionAcceptanceRejection = Literal[
    "suggestion_not_found",
    "state_unavailable",
    "not_allowed",
    "already_processing",
    "attachment_unavailable",
    "creation_conflict",
]


@dataclass(frozen=True, slots=True)
class SuggestionAcceptanceRejected:
    reason: SuggestionAcceptanceRejection


@dataclass(frozen=True, slots=True)
class SuggestionAccepted:
    """The approval is recorded, now or earlier by the same ``command_id``."""

    accepted_at: str
    action: SubmitActionMessageResult


type SuggestionAcceptanceResult = SuggestionAccepted | SuggestionAcceptanceRejected


async def accept_suggestion(
    *,
    user_id: str,
    suggestion_id: str,
    command_id: str,
    approval_mode: ApprovalMode,
    language: ActionLanguage | None,
    supplement: str | None,
    supplement_project_refs: tuple[ActionProjectRef, ...],
    images: tuple[ImageInput, ...],
    files: tuple[FileAttachmentInput, ...],
    chat_handoff: ChatHandoffInput | None = None,
) -> SuggestionAcceptanceResult:
    """Approve one ``action_offer`` Suggestion and create its Action once.

    Repeating the same ``command_id`` replays the recorded approval; any other
    command on a Suggestion that already has a reaction is rejected. The
    approval and the Action are written in one transaction by
    ``submit_action_message``; its unexpected failures propagate to the caller,
    which owns the response.
    """

    try:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        state = await LocalSuggestionStateRepository(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ).get_suggestion_state(user_id=user_id, suggestion_id=suggestion_id)
    except (MigrationError, OSError, sqlite3.Error):
        logger.exception("Suggestion read failed: %s", suggestion_id)
        return SuggestionAcceptanceRejected("state_unavailable")
    row = state.data
    if row is None:
        return SuggestionAcceptanceRejected("suggestion_not_found")
    rejection = _state_rejection(row, command_id=command_id)
    if rejection is not None:
        return SuggestionAcceptanceRejected(rejection)

    # A replayed command must carry the approval time it was first recorded with.
    accepted_at = _optional_text(row.get("accepted_at")) or now_utc_iso()
    try:
        result = submit_action_message(
            _build_command(
                user_id=user_id,
                suggestion_id=suggestion_id,
                command_id=command_id,
                approved_at=accepted_at,
                language=language,
                supplement=_optional_text(supplement),
                supplement_project_refs=supplement_project_refs,
                approval_mode=approval_mode,
                images=images,
                files=files,
                chat_handoff=chat_handoff,
                suggestion_row=row,
            )
        )
    except ActionFileAttachmentUnavailableError:
        return SuggestionAcceptanceRejected("attachment_unavailable")
    except ActionMessageConflictError:
        return SuggestionAcceptanceRejected("creation_conflict")
    return SuggestionAccepted(accepted_at=accepted_at, action=result)


def _state_rejection(
    row: Mapping[str, object], *, command_id: str
) -> SuggestionAcceptanceRejection | None:
    if _optional_text(row.get("interaction_contract")) != "action_offer":
        return "not_allowed"
    user_reaction = row.get("user_reaction")
    if user_reaction is None:
        return None
    if user_reaction == "accepted" and row.get("action_command_id") == command_id:
        return None
    if row.get("action_status") == ACTION_STATUS_PROCESSING:
        return "already_processing"
    return "not_allowed"


def _target_metadata(row: Mapping[str, object]) -> tuple[str | None, str | None]:
    target_context = row.get("target_context_json")
    if not isinstance(target_context, Mapping):
        return None, None
    return (
        _optional_text(target_context.get("organization_name")),
        _optional_text(target_context.get("project_name")),
    )


def _build_command(
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
    chat_handoff: ChatHandoffInput | None,
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
                chat_handoff=chat_handoff,
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


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None

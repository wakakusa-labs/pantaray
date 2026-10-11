"""Backend-owned overlay bootstrap service."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from pantaray_agents.action_status import (
    ACTION_STATUS_CANCELED,
    ACTION_STATUS_ERROR,
    ACTION_STATUS_IDLE,
    ACTION_STATUS_PROCESSING,
    ACTION_TERMINAL_STATUSES,
    ActionRuntimeStatus,
    derive_action_phase,
    parse_stored_suggestion_user_reaction,
)
from pantaray_agents.local_runtime.suggestion_state.public_projection import (
    FoldedSuggestionState,
    fold_suggestion_state_from_events,
)
from pantaray_agents.orchestration.common.normalization import (
    normalize_lower_string,
    normalize_string,
)
from pantaray_agents.orchestration.session.live_action_processes import (
    LiveActionProcessKey,
)
from pantaray_agents.repositories.runtime_ports import SuggestionRepositoryPort
from pantaray_agents.schema.overlay_bootstrap import (
    OverlayBootstrapResponse,
    OverlayLiveResumeModel,
    OverlaySnapshotModel,
)
from pantaray_agents.schema.repositories.repository import DBRow


class OverlayBootstrapError(RuntimeError):
    """Base error for overlay bootstrap failures."""


class OverlayBootstrapNotFoundError(OverlayBootstrapError):
    """Suggestion was not found for the authenticated user."""


class OverlayBootstrapMissingEventLogError(OverlayBootstrapError):
    """Legacy compatibility placeholder."""


class OverlayBootstrapInvariantError(OverlayBootstrapError):
    """Durable read model is malformed or inconsistent."""


def _normalize_action_status(value: object) -> ActionRuntimeStatus | None:
    lowered = normalize_lower_string(value)
    if lowered is None:
        return None
    if lowered == ACTION_STATUS_IDLE:
        return ACTION_STATUS_IDLE
    if lowered == ACTION_STATUS_PROCESSING:
        return ACTION_STATUS_PROCESSING
    if lowered in ACTION_TERMINAL_STATUSES:
        return cast(ActionRuntimeStatus, lowered)
    raise OverlayBootstrapInvariantError(
        f"Unsupported action status in overlay bootstrap: {value!r}"
    )


def _derive_reaction_timestamp(history_row: DBRow) -> str | None:
    reaction = parse_stored_suggestion_user_reaction(history_row.get("user_reaction"))
    if reaction == "accepted":
        return normalize_string(history_row.get("accepted_at"))
    if reaction == "rejected":
        return normalize_string(history_row.get("rejected_at"))
    return None


def _build_live_resume(
    *,
    live_action_key: LiveActionProcessKey | None,
    accepted_at: str | None,
    action_phase: str,
    action_status: ActionRuntimeStatus | None,
) -> OverlayLiveResumeModel:
    if (
        action_phase == "processing"
        and action_status == "processing"
        and live_action_key is not None
    ):
        return OverlayLiveResumeModel(
            kind="action",
            process_id=live_action_key.process_id,
            action_id=live_action_key.action_id,
            command_id=live_action_key.command_id,
            accepted_at=accepted_at,
        )
    return OverlayLiveResumeModel(
        kind="none",
        process_id=None,
        action_id=None,
        command_id=None,
        accepted_at=None,
    )


def _derive_reaction_timestamp_from_fold(folded: FoldedSuggestionState) -> str | None:
    reaction = parse_stored_suggestion_user_reaction(folded.user_reaction)
    if reaction == "accepted":
        return folded.accepted_at
    if reaction == "rejected":
        return folded.rejected_at
    return None


def _merge_canonical_action_state(
    *,
    folded: FoldedSuggestionState,
    suggestion_row: DBRow,
) -> FoldedSuggestionState:
    canonical_status = _normalize_action_status(suggestion_row.get("action_status"))
    canonical_action_id = normalize_string(suggestion_row.get("action_id"))
    canonical_command_id = normalize_string(suggestion_row.get("action_command_id"))
    canonical_process_id = normalize_string(suggestion_row.get("action_process_id"))
    comparisons = (
        ("action_status", folded.action_status, canonical_status),
        ("action_id", folded.action_id, canonical_action_id),
        ("command_id", folded.command_id, canonical_command_id),
        ("process_id", folded.process_id, canonical_process_id),
    )
    for field_name, folded_value, canonical_value in comparisons:
        if folded_value is not None and folded_value != canonical_value:
            raise OverlayBootstrapInvariantError(
                f"Folded {field_name} does not match canonical Action state"
            )
    return replace(
        folded,
        action_status=canonical_status,
        action_id=canonical_action_id,
        command_id=canonical_command_id,
        process_id=canonical_process_id,
    )


def _build_snapshot(
    *,
    suggestion_row: DBRow,
    folded: FoldedSuggestionState,
) -> OverlaySnapshotModel:
    suggestion_id = folded.suggestion_id
    reaction = parse_stored_suggestion_user_reaction(folded.user_reaction)
    action_status = _normalize_action_status(folded.action_status)
    process_status = normalize_lower_string(suggestion_row.get("action_process_status"))
    job_status = normalize_lower_string(suggestion_row.get("action_job_status"))
    action_phase = derive_action_phase(
        user_reaction=reaction,
        action_status=action_status,
        process_status=process_status,
        job_status=job_status,
    )
    action_failure_code = normalize_string(folded.action_failure_code)
    action_failure_stage = normalize_string(folded.action_failure_stage)
    action_failure_message_public = normalize_string(
        folded.action_failure_message_public
    )
    last_sequence = folded.last_sequence
    updated_at = normalize_string(folded.updated_at)

    if action_status in {ACTION_STATUS_ERROR, ACTION_STATUS_CANCELED}:
        if action_failure_stage is None or action_failure_message_public is None:
            raise OverlayBootstrapInvariantError(
                "Terminal failure action history row is missing failure details"
            )
        if action_failure_code is None:
            raise OverlayBootstrapInvariantError(
                "Terminal failure action history row is missing action_failure_code"
            )

    return OverlaySnapshotModel(
        suggestionId=suggestion_id,
        commandId=folded.command_id,
        interactionContract=normalize_string(folded.interaction_contract),
        suggestionText=normalize_string(folded.suggestion_text) or "",
        reactionState=reaction,  # type: ignore[arg-type]
        reactionTimestamp=_derive_reaction_timestamp_from_fold(folded),
        dismissReason=normalize_string(suggestion_row.get("dismiss_reason")),
        actionPhase=action_phase,  # type: ignore[arg-type]
        actionStatus=action_status,
        actionFailureCode=action_failure_code,
        actionFailureStage=action_failure_stage,
        actionFailureMessagePublic=action_failure_message_public,
        processId=folded.process_id,
        actionId=folded.action_id,
        updatedAt=updated_at,
        lastSequence=last_sequence,
        isLive=action_phase == "processing",
    )


class OverlayBootstrapService:
    """Build an overlay snapshot from durable current state and UI read model."""

    def __init__(
        self,
        repository: SuggestionRepositoryPort,
    ) -> None:
        self._repository = repository

    async def build_for_suggestion(
        self,
        *,
        user_id: str,
        suggestion_id: str,
    ) -> OverlayBootstrapResponse:
        suggestion_result = await self._repository.get_suggestion_state(
            user_id=str(user_id),
            suggestion_id=str(suggestion_id),
        )
        if suggestion_result.error:
            raise OverlayBootstrapError(suggestion_result.error)
        suggestion_row = (
            suggestion_result.data if isinstance(suggestion_result.data, dict) else None
        )
        if suggestion_row is None:
            raise OverlayBootstrapNotFoundError("Suggestion not found")

        events_result = await self._repository.get_process_events_for_detail(
            user_id=str(user_id),
            suggestion_id=str(suggestion_id),
        )
        if events_result.error:
            raise OverlayBootstrapError(events_result.error)
        event_rows = events_result.data if isinstance(events_result.data, list) else []
        if not event_rows:
            raise OverlayBootstrapMissingEventLogError("Suggestion event log not found")
        try:
            folded = fold_suggestion_state_from_events(
                suggestion_id=str(suggestion_id),
                rows=event_rows,
            )
        except ValueError as exc:
            raise OverlayBootstrapInvariantError(str(exc)) from exc
        folded = _merge_canonical_action_state(
            folded=folded,
            suggestion_row=suggestion_row,
        )

        live_action_key = LiveActionProcessKey.from_durable_action(
            user_id=str(user_id),
            suggestion_id=str(suggestion_id),
            action_row=suggestion_row,
        )
        accepted_at = (
            normalize_string(suggestion_row.get("accepted_at"))
            if live_action_key is not None
            else None
        )
        snapshot = _build_snapshot(
            suggestion_row=suggestion_row,
            folded=folded,
        )
        live_resume = _build_live_resume(
            live_action_key=live_action_key,
            accepted_at=accepted_at,
            action_phase=snapshot.actionPhase,
            action_status=snapshot.actionStatus,
        )
        return OverlayBootstrapResponse(
            suggestion_id=str(suggestion_id),
            snapshot=snapshot,
            last_sequence=snapshot.lastSequence,
            live_resume=live_resume,
        )

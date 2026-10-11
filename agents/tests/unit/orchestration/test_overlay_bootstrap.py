from __future__ import annotations

import pytest

from pantaray_agents.orchestration.overlay.bootstrap import (
    OverlayBootstrapInvariantError,
    OverlayBootstrapMissingEventLogError,
    OverlayBootstrapService,
)
from pantaray_agents.schema.repositories.repository import RepositoryResult


class _SuggestionRepo:
    def __init__(
        self,
        *,
        suggestion_row: dict[str, object] | None,
        event_rows: list[dict[str, object]] | None,
    ) -> None:
        self._suggestion_row = suggestion_row
        self._event_rows = event_rows

    async def get_suggestion_state(self, *, user_id: str, suggestion_id: str):
        row = self._suggestion_row
        if row is None:
            return RepositoryResult(data=None)
        if (
            str(row.get("user_id")) != user_id
            or str(row.get("suggestion_id")) != suggestion_id
        ):
            return RepositoryResult(data=None)
        return RepositoryResult(data=dict(row))

    async def get_process_events_for_detail(self, *, user_id: str, suggestion_id: str):
        rows = self._event_rows or []
        filtered = [
            dict(row)
            for row in rows
            if str(row.get("user_id")) == user_id
            and str(row.get("suggestion_id")) == suggestion_id
        ]
        return RepositoryResult(data=filtered)


def _event(
    *,
    sequence: int,
    event_name: str,
    created_at: str,
    data: dict[str, object],
    process_id: str = "process-123",
    suggestion_id: str = "sug-123",
    user_id: str = "user-123",
    action_id: str | None = "action-123",
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "event_name": event_name,
        "created_at": created_at,
        "process_id": process_id,
        "suggestion_id": suggestion_id,
        "user_id": user_id,
        "action_id": action_id,
        "payload": {
            "data": data,
            "meta": {
                "process_id": process_id,
                "suggestion_id": suggestion_id,
                "action_id": action_id,
            },
        },
    }


def _overlay_service(repo: _SuggestionRepo) -> OverlayBootstrapService:
    return OverlayBootstrapService(repo)


async def test_overlay_bootstrap_uses_history_row_as_initial_snapshot() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "success",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=4,
                event_name="process_started",
                created_at="2026-03-14T00:00:04Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=8,
                event_name="process_completed",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "status": "success",
                    "completed_at": "2026-03-14T00:00:05Z",
                    "final_output": "action final output",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.suggestionText == "first line"
    assert response.snapshot.interactionContract == "action_offer"
    assert response.snapshot.reactionState == "accepted"
    assert response.snapshot.reactionTimestamp == "2026-03-14T00:00:03Z"
    assert response.snapshot.actionPhase == "terminal"
    assert response.snapshot.actionStatus == "success"
    assert response.snapshot.actionId == "action-123"
    assert response.snapshot.commandId == "command-123"
    assert response.snapshot.lastSequence == 8
    assert response.live_resume.kind == "none"


async def test_overlay_bootstrap_surfaces_failure_reason_for_terminal_error() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "error",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=8,
                event_name="process_completed",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "status": "error",
                    "completed_at": "2026-03-14T00:00:05Z",
                    "failure_code": "ACTION_PROCESSING_ERROR",
                    "failure_stage": "running_failed",
                    "failure_message_public": "Action execution failed.",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.actionPhase == "terminal"
    assert response.snapshot.actionStatus == "error"
    assert response.snapshot.actionFailureCode == "ACTION_PROCESSING_ERROR"
    assert response.snapshot.actionFailureStage == "running_failed"
    assert response.snapshot.actionFailureMessagePublic == "Action execution failed."


async def test_overlay_bootstrap_rejects_unsupported_action_status() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "timeout",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:04Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "status": "timeout",
                },
            ),
        ],
    )

    with pytest.raises(
        OverlayBootstrapInvariantError,
        match="Unsupported action status",
    ):
        await _overlay_service(repo).build_for_suggestion(
            user_id="user-123",
            suggestion_id="sug-123",
        )


async def test_overlay_bootstrap_resumes_running_action_without_memory() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "processing",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_process_status": "running",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=5,
                event_name="process_started",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123", suggestion_id="sug-123"
    )

    assert response.snapshot.actionPhase == "processing"
    assert response.snapshot.actionStatus == "processing"
    assert response.live_resume.kind == "action"
    assert response.live_resume.process_id == "process-123"
    assert response.live_resume.action_id == "action-123"
    assert response.live_resume.command_id == "command-123"


async def test_overlay_bootstrap_recovers_processing_action_when_process_started_kind_is_missing() -> (
    None
):
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "processing",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_process_status": "running",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=2,
                event_name="process_started",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123", suggestion_id="sug-123"
    )

    assert response.snapshot.actionPhase == "processing"
    assert response.snapshot.actionStatus == "processing"
    assert response.live_resume.kind == "action"
    assert response.live_resume.process_id == "process-123"
    assert response.live_resume.action_id == "action-123"
    assert response.live_resume.command_id == "command-123"


async def test_overlay_bootstrap_resumes_durable_paused_approval_without_memory() -> (
    None
):
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "processing",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_process_status": "paused",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_started",
                created_at="2026-03-14T00:00:05Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=3,
                event_name="process_paused",
                created_at="2026-03-14T00:00:06Z",
                data={
                    "kind": "action",
                    "reason": "approval_pending",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "approval_blockers": [
                        {
                            "approval_session_id": "approval-123",
                            "tool_request_id": "tool-request-123",
                            "tool_id": "apply_patch",
                            "intent_class": "surgical_edit",
                            "command_summary": {"summary": "Edit todo.txt"},
                        }
                    ],
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.live_resume.kind == "action"
    assert response.live_resume.process_id == "process-123"
    assert response.live_resume.action_id == "action-123"
    assert response.live_resume.command_id == "command-123"


async def test_overlay_bootstrap_restores_canceled_terminal_after_approval_denial() -> (
    None
):
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "canceled",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
            _event(
                sequence=6,
                event_name="process_completed",
                created_at="2026-03-14T00:00:06Z",
                data={
                    "kind": "action",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "command_id": "command-123",
                    "status": "canceled",
                    "failure_code": "ACTION_APPROVAL_DENIED",
                    "failure_stage": "approval_denied",
                    "failure_message_public": "The action was canceled because approval was denied.",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.actionStatus == "canceled"
    assert response.snapshot.actionFailureStage == "approval_denied"


@pytest.mark.parametrize(
    ("process_status", "job_status"),
    [
        ("enqueued", "queued"),
        ("running", "running"),
    ],
)
async def test_overlay_bootstrap_derives_pending_start_from_db_lane_with_null_payload(
    process_status: str,
    job_status: str,
) -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "idle",
            "action_request_payload": None,
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_process_status": process_status,
            "action_job_status": job_status,
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=4,
                event_name="suggestion_reaction_committed",
                created_at="2026-03-14T00:00:03Z",
                action_id=None,
                data={
                    "reaction": "accepted",
                    "committed_at": "2026-03-14T00:00:03Z",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.actionPhase == "accepted_pending_start"
    assert response.snapshot.actionStatus == "idle"
    assert response.snapshot.reactionState == "accepted"
    assert response.snapshot.actionId == "action-123"
    assert response.snapshot.commandId == "command-123"
    assert response.snapshot.processId == "process-123"
    assert response.live_resume.kind == "none"


async def test_overlay_bootstrap_rejects_folded_action_identity_mismatch() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "idle",
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_command_id": "command-123",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                action_id="other-action",
                data={
                    "command_id": "command-123",
                    "process_id": "process-123",
                    "action_id": "other-action",
                },
            )
        ],
    )

    with pytest.raises(OverlayBootstrapInvariantError, match="action_id"):
        await _overlay_service(repo).build_for_suggestion(
            user_id="user-123",
            suggestion_id="sug-123",
        )


async def test_overlay_bootstrap_does_not_infer_pending_start_from_legacy_payload() -> (
    None
):
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "idle",
            "action_request_payload": {
                "version": 1,
                "command_id": "command-123",
                "screen_captures": [],
            },
            "action_id": None,
            "action_process_id": None,
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=4,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                action_id=None,
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.actionPhase == "idle"
    assert response.snapshot.actionStatus == "idle"
    assert response.live_resume.kind == "none"


async def test_overlay_bootstrap_maps_legacy_start_retry_event_to_terminal_error() -> (
    None
):
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "error",
            "action_request_payload": {
                "version": 1,
                "command_id": "command-123",
                "screen_captures": [],
            },
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_process_status": "enqueued",
            "action_job_status": "failed",
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                action_id="action-123",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                    "process_id": "process-123",
                    "action_id": "action-123",
                },
            ),
            _event(
                sequence=4,
                event_name="action_start_retry_required",
                created_at="2026-03-14T00:00:04Z",
                action_id="action-123",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                    "process_id": "process-123",
                    "action_id": "action-123",
                    "failure_code": "WS_DEPENDENCY_UNAVAILABLE",
                    "failure_stage": "load_suggestion_state",
                    "failure_message_public": "dependency failed",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.actionPhase == "terminal"
    assert response.snapshot.actionStatus == "error"
    assert response.snapshot.actionFailureCode == "WS_DEPENDENCY_UNAVAILABLE"


async def test_overlay_bootstrap_returns_idle_when_start_lane_is_missing() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "idle",
            "action_request_payload": {
                "version": 1,
                "command_id": "command-123",
                "screen_captures": [],
            },
            "action_id": "action-123",
            "action_process_id": "process-123",
            "action_process_status": None,
            "action_job_status": None,
            "action_command_id": "command-123",
            "accepted_at": "2026-03-14T00:00:03Z",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="process_completed",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                process_id="process-suggestion-123",
                data={
                    "kind": "suggestion",
                    "status": "success",
                    "interaction_contract": "action_offer",
                },
            ),
            _event(
                sequence=3,
                event_name="action_requested",
                created_at="2026-03-14T00:00:03Z",
                action_id="action-123",
                data={
                    "suggestion_id": "sug-123",
                    "command_id": "command-123",
                    "accepted_at": "2026-03-14T00:00:03Z",
                    "process_id": "process-123",
                    "action_id": "action-123",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.actionPhase == "idle"
    assert response.snapshot.actionStatus == "idle"


async def test_overlay_bootstrap_carries_the_reason_recorded_with_a_dismissal() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": "idle",
            "user_reaction": "rejected",
            "rejected_at": "2026-03-14T00:00:03Z",
            "dismiss_reason": "Already done by hand.",
        },
        event_rows=[
            _event(
                sequence=1,
                event_name="suggestion_chunk",
                created_at="2026-03-14T00:00:02Z",
                action_id=None,
                data={"content": "first line"},
            ),
            _event(
                sequence=2,
                event_name="suggestion_reaction_committed",
                created_at="2026-03-14T00:00:03Z",
                action_id=None,
                data={
                    "suggestion_id": "sug-123",
                    "reaction": "rejected",
                    "committed_at": "2026-03-14T00:00:03Z",
                    "reason": "Already done by hand.",
                },
            ),
        ],
    )

    response = await _overlay_service(repo).build_for_suggestion(
        user_id="user-123",
        suggestion_id="sug-123",
    )

    assert response.snapshot.reactionState == "rejected"
    assert response.snapshot.dismissReason == "Already done by hand."


async def test_overlay_bootstrap_requires_history_sequence() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": None,
        },
        event_rows=[
            {
                "sequence": None,
                "event_name": "suggestion_chunk",
                "created_at": "2026-03-14T00:00:02Z",
                "process_id": "process-suggestion-123",
                "suggestion_id": "sug-123",
                "user_id": "user-123",
                "action_id": None,
                "payload": {"data": {"content": "first line"}, "meta": {}},
            }
        ],
    )

    try:
        await _overlay_service(repo).build_for_suggestion(
            user_id="user-123",
            suggestion_id="sug-123",
        )
    except OverlayBootstrapInvariantError:
        pass
    else:
        raise AssertionError("Expected invalid history row to fail closed")


async def test_overlay_bootstrap_raises_not_found_without_history_row() -> None:
    repo = _SuggestionRepo(
        suggestion_row={
            "suggestion_id": "sug-123",
            "user_id": "user-123",
            "action_status": None,
        },
        event_rows=None,
    )

    try:
        await _overlay_service(repo).build_for_suggestion(
            user_id="user-123",
            suggestion_id="sug-123",
        )
    except OverlayBootstrapMissingEventLogError:
        pass
    else:
        raise AssertionError("Expected missing event log to fail closed")

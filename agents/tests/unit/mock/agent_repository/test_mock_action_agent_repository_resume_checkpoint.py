from datetime import UTC, datetime

import pytest

from .shared import MockActionAgentRepository, StatusType, StepType


def _action_record(*, action_id: str, user_id: str) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "action_id": action_id,
        "suggestion_id": f"suggestion-{action_id}",
        "user_id": user_id,
        "final_output": "",
        "status": StatusType.PROCESSING,
        "prompt_name": "action/executing",
        "prompt_version": "test",
        "created_at": now,
        "updated_at": now,
    }


async def _save_action(
    repo: MockActionAgentRepository,
    *,
    action_id: str,
    user_id: str,
) -> None:
    result = await repo.save_action(
        _action_record(action_id=action_id, user_id=user_id),
        prompt_name="action/executing",
        prompt_version="test",
    )
    assert result.error is None


@pytest.mark.asyncio
async def test_get_runtime_resume_context_matches_user_resume_contract() -> None:
    repo = MockActionAgentRepository()
    await _save_action(repo, action_id="action-user", user_id="user-1")
    repo.data["action_steps"] = [
        {
            "step_id": "checkpoint-1",
            "action_id": "action-user",
            "user_id": "user-1",
            "step_number": 1,
            "runtime_state_checkpoint": {"step": 2},
            "completed_at": "2026-03-24T00:01:00Z",
            "created_at": "2026-03-24T00:01:00Z",
        },
        {
            "step_id": "user-2",
            "action_id": "action-user",
            "user_id": "user-1",
            "accepted_sequence": 1,
            "step_number": 2,
            "local_step_number": 2,
            "short_step_id": "S-2-USER",
            "step_type": "user_request",
            "status": "success",
            "user_message_id": "message-2",
            "user_message_json": (
                '{"version":1,"message_id":"message-2","content":"Second",'
                '"images":[{"kind":"image","storage_path":"users/user-1/images/generic.png"},'
                '{"kind":"image","storage_path":"users/user-1/images/screen.png"}],'
                '"suggestion_approval":{"suggestion_id":"suggestion-action-user",'
                '"approved_at":"2026-03-24T00:01:00Z","summary":"Prior suggestion",'
                '"organization_name":"Wakakusa","project_name":"Pantaray"}}'
            ),
            "user_request_text": (
                "Second\n\nSuggestion metadata:\n"
                "- Suggestion: suggestion-action-user\n"
                "- Suggestion summary: Prior suggestion\n"
                "- Organization: Wakakusa\n"
                "- Project: Pantaray\n"
                "- Approved at: 2026-03-24T00:01:00Z"
            ),
            "adopted_process_id": "process-2",
            "created_at": "2026-03-24T00:02:00Z",
        },
    ]

    result = await repo.get_runtime_resume_context_for_user_step(
        user_id="user-1",
        action_id="action-user",
        current_user_step_number=3,
    )

    assert result.data is not None
    assert result.data.checkpoint_row is not None
    assert result.data.checkpoint_row["step_id"] == "checkpoint-1"
    (intervening,) = result.data.intervening_user_steps
    assert intervening.step_id == "user-2"
    message = intervening.message
    assert message.suggestion_approval is not None
    assert message.suggestion_approval.model_dump() == {
        "suggestion_id": "suggestion-action-user",
        "approved_at": "2026-03-24T00:01:00Z",
        "summary": "Prior suggestion",
        "organization_name": "Wakakusa",
        "project_name": "Pantaray",
    }
    assert tuple(image.model_dump(mode="json") for image in message.images) == (
        {"kind": "image", "storage_path": "users/user-1/images/generic.png"},
        {"kind": "image", "storage_path": "users/user-1/images/screen.png"},
    )

    repo.data["action_steps"].append(
        {
            **repo.data["action_steps"][1],
            "step_id": "user-3",
            "step_number": 3,
            "local_step_number": 3,
            "short_step_id": "S-3-USER",
        }
    )
    several = await repo.get_runtime_resume_context_for_user_step(
        user_id="user-1",
        action_id="action-user",
        current_user_step_number=4,
    )
    assert several.data is not None
    assert [step.step_id for step in several.data.intervening_user_steps] == [
        "user-2",
        "user-3",
    ]

    repo.data["action_steps"].append(
        {
            "step_id": "checkpoint-current-run",
            "action_id": "action-user",
            "user_id": "user-1",
            "step_number": 4,
            "runtime_state_checkpoint": {"step": 5},
            "created_at": "2026-03-24T00:04:00Z",
        }
    )
    retry_result = await repo.get_runtime_resume_context_for_user_step(
        user_id="user-1",
        action_id="action-user",
        current_user_step_number=3,
    )
    assert retry_result.data is not None
    assert retry_result.data.checkpoint_row is not None
    assert retry_result.data.checkpoint_row["step_id"] == "checkpoint-current-run"
    assert retry_result.data.intervening_user_steps == ()


@pytest.mark.asyncio
async def test_get_runtime_checkpoint_for_approval_resume_returns_latest_after_anchor() -> (
    None
):
    repo = MockActionAgentRepository()
    await _save_action(repo, action_id="action-blocker", user_id="user-blocker")

    matching = await repo.save_action_step(
        step_id="step-matching-blocker",
        action_id="action-blocker",
        step_number=1,
        step_name="tool::bash",
        step_type=StepType.TOOL_EXECUTION,
        runtime_state_checkpoint={
            "current_approval_blockers": [
                {
                    "approval_session_id": "approval-session-1",
                    "tool_request_id": "tool-request-1",
                }
            ]
        },
        status="success",
        goal_handle="S",
        user_id="user-blocker",
        short_step_id="S-1-TOOL",
        local_step_number=1,
    )
    assert matching.error is None

    later_non_matching = await repo.save_action_step(
        step_id="step-later-non-matching",
        action_id="action-blocker",
        step_number=2,
        step_name="goal_worker::think",
        step_type=StepType.LLM_OUTPUT,
        runtime_state_checkpoint={"current_approval_blockers": []},
        status="success",
        goal_handle="G1",
        user_id="user-blocker",
        short_step_id="G1-2-THINK",
        local_step_number=2,
    )
    assert later_non_matching.error is None

    result = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id="user-blocker",
        action_id="action-blocker",
        approval_session_id="approval-session-1",
        tool_request_id="tool-request-1",
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["step_id"] == "step-later-non-matching"
    assert result.metadata == {"approval_anchor_step_id": "step-matching-blocker"}


@pytest.mark.asyncio
async def test_get_runtime_checkpoint_for_approval_resume_matches_pending_request() -> (
    None
):
    repo = MockActionAgentRepository()
    await _save_action(repo, action_id="action-pending", user_id="user-pending")

    saved = await repo.save_action_step(
        step_id="step-pending-request",
        action_id="action-pending",
        step_number=1,
        step_name="tool::bash",
        step_type=StepType.TOOL_EXECUTION,
        runtime_state_checkpoint={
            "pending_approval_request": {
                "approval_session_id": "approval-session-2",
                "tool_request_id": "tool-request-2",
            },
            "current_approval_blockers": [],
        },
        status="success",
        goal_handle="S",
        user_id="user-pending",
        short_step_id="S-1-TOOL",
        local_step_number=1,
    )
    assert saved.error is None

    result = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id="user-pending",
        action_id="action-pending",
        approval_session_id="approval-session-2",
        tool_request_id="tool-request-2",
    )

    assert result.error is None
    assert result.data is not None
    assert result.data["step_id"] == "step-pending-request"


@pytest.mark.asyncio
async def test_get_runtime_checkpoint_for_approval_resume_returns_none_on_miss() -> (
    None
):
    repo = MockActionAgentRepository()
    await _save_action(repo, action_id="action-miss", user_id="user-miss")

    saved = await repo.save_action_step(
        step_id="step-unrelated",
        action_id="action-miss",
        step_number=1,
        step_name="tool::bash",
        step_type=StepType.TOOL_EXECUTION,
        runtime_state_checkpoint={
            "current_approval_blockers": [
                {
                    "approval_session_id": "other-approval-session",
                    "tool_request_id": "other-tool-request",
                }
            ]
        },
        status="success",
        goal_handle="S",
        user_id="user-miss",
        short_step_id="S-1-TOOL",
        local_step_number=1,
    )
    assert saved.error is None

    result = await repo.get_runtime_checkpoint_for_approval_resume(
        user_id="user-miss",
        action_id="action-miss",
        approval_session_id="approval-session-3",
        tool_request_id="tool-request-3",
    )

    assert result.error is None
    assert result.data is None

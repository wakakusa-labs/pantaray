from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException, status

from pantaray_agents.local_runtime.runtime.action_approval_decision import (
    ActionApprovalDecisionCommand,
    ActionApprovalDecisionError,
    ActionApprovalDecisionResult,
)
from pantaray_agents.local_runtime.runtime.job_types import ACTION_PROCESS_KIND
from pantaray_agents.routers import action_approval


@pytest.fixture(autouse=True)
def _root_process_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every request in these tests to the root Action process."""

    monkeypatch.setattr(
        action_approval,
        "verify_current_owner",
        lambda _user_id: None,
    )
    monkeypatch.setattr(
        action_approval,
        "read_local_runtime_db_config",
        lambda: (Path("/tmp/runtime.db"), 1000),
    )
    monkeypatch.setattr(
        action_approval,
        "_read_approval_process_kind",
        lambda **_kwargs: ACTION_PROCESS_KIND,
    )


@pytest.mark.asyncio
async def test_decide_action_tool_approval_uses_action_only_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ActionApprovalDecisionCommand] = []

    def apply(command: ActionApprovalDecisionCommand) -> ActionApprovalDecisionResult:
        captured.append(command)
        return ActionApprovalDecisionResult(
            approval_session_id="approval-1",
            process_id="process-1",
            job_id="job-2",
            decision="approved_once",
        )

    monkeypatch.setattr(action_approval, "apply_action_approval_decision", apply)

    response = await action_approval.decide_action_tool_approval(
        user_id="user-1",
        action_id="action-1",
        body=action_approval.ActionApprovalDecisionRequest(
            decision="approved_once",
            process_id="process-1",
            tool_request_id="tool-request-1",
            approval_session_id="approval-1",
        ),
        resolved_user_id="user-1",
    )

    assert response.accepted is True
    assert response.process_id == "process-1"
    assert response.decision == "approved_once"
    assert len(captured) == 1
    command = captured[0]
    assert command.user_id == "user-1"
    assert command.action_id == "action-1"
    assert command.process_id == "process-1"
    assert command.tool_request_id == "tool-request-1"


@pytest.mark.asyncio
async def test_decide_action_tool_approval_rejects_other_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def apply(_command: ActionApprovalDecisionCommand) -> ActionApprovalDecisionResult:
        nonlocal called
        called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(action_approval, "apply_action_approval_decision", apply)

    with pytest.raises(HTTPException) as exc_info:
        await action_approval.decide_action_tool_approval(
            user_id="user-1",
            action_id="action-1",
            body=action_approval.ActionApprovalDecisionRequest(
                decision="denied",
                process_id="process-1",
                tool_request_id="tool-request-1",
                approval_session_id="approval-1",
            ),
            resolved_user_id="other-user",
        )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("APPROVAL_NOT_FOUND", status.HTTP_404_NOT_FOUND),
        ("APPROVAL_REQUEST_MISMATCH", status.HTTP_422_UNPROCESSABLE_CONTENT),
        ("APPROVAL_DECISION_CONFLICT", status.HTTP_409_CONFLICT),
        ("APPROVAL_INTERRUPTED", status.HTTP_409_CONFLICT),
    ],
)
async def test_decide_action_tool_approval_maps_canonical_errors(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    expected_status: int,
) -> None:
    def fail(_command: ActionApprovalDecisionCommand) -> ActionApprovalDecisionResult:
        raise ActionApprovalDecisionError("approval failed", error_code=error_code)

    monkeypatch.setattr(action_approval, "apply_action_approval_decision", fail)

    with pytest.raises(HTTPException) as exc_info:
        await action_approval.decide_action_tool_approval(
            user_id="user-1",
            action_id="action-1",
            body=action_approval.ActionApprovalDecisionRequest(
                decision="approved_once",
                process_id="process-1",
                tool_request_id="tool-request-1",
                approval_session_id="approval-1",
            ),
            resolved_user_id="user-1",
        )

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.detail["error_code"] == error_code

"""The approval POST dispatches by the immutable physical ``processes.kind``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from fastapi import HTTPException, status

from pantaray_agents.local_runtime.runtime.action_approval_decision import (
    ActionApprovalDecisionCommand,
    ActionApprovalDecisionResult,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    mark_local_action_job_paused,
)
from pantaray_agents.routers import action_approval
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from . import test_action_pending_approval_snapshot as snapshot
from . import test_action_subagent_spawn as spawn

ROOT_SESSION_ID = "session-root"
ROOT_REQUEST_ID = "request-root"


@pytest.fixture(autouse=True)
def _reset_owner_state() -> None:
    yield
    reset_logged_out_owner()


def _paused_action(
    tmp_path: Path,
) -> tuple[Path, ActionSubagentJobPayload, ActionSubagentJobPayload, str, str]:
    """Pause both children and the root, each on its own approval blocker."""

    db_path, child_a, child_b = snapshot._running_children(tmp_path)
    session_a, request_a = snapshot._pause_child(db_path, child_a, name="a")
    snapshot._pause_child(db_path, child_b, name="b")
    snapshot._insert_pending_session(
        db_path, session_id=ROOT_SESSION_ID, request_id=ROOT_REQUEST_ID
    )
    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "INSERT INTO job_attempts(attempt_id,job_id,attempt_number,started_at,"
            "status) VALUES ('parent-attempt',?,1,?,'running')",
            (snapshot.ROOT_JOB_ID, spawn.TIMESTAMP),
        )
    mark_local_action_job_paused(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        job_id=snapshot.ROOT_JOB_ID,
        process_id=snapshot.ROOT_PROCESS_ID,
        payload={
            "action_id": "action-1",
            "user_id": "user-1",
            "status": "processing",
            "completed_at": spawn.TIMESTAMP,
            "reason": "approval_pending",
            "approval_blockers": [
                {
                    "approval_session_id": ROOT_SESSION_ID,
                    "tool_request_id": ROOT_REQUEST_ID,
                }
            ],
        },
    )
    return db_path, child_a, child_b, session_a, request_a


def _bind_runtime(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    monkeypatch.setattr(
        action_approval,
        "read_local_runtime_db_config",
        lambda: (db_path, spawn.BUSY_TIMEOUT_MS),
    )
    # Approving as the logged-out owner exercises the real owner check.
    register_logged_out_owner("user-1")


def _session_statuses(db_path: Path) -> dict[str, str]:
    with spawn._connect(db_path) as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT approval_session_id,status FROM approval_sessions"
            ).fetchall()
        }


async def _decide(
    process_id: str,
    session_id: str,
    request_id: str,
    decision: Literal[
        "approved_once", "approved_for_conversation", "denied"
    ] = "approved_once",
) -> action_approval.ActionApprovalDecisionResponse:
    return await action_approval.decide_action_tool_approval(
        user_id="user-1",
        action_id="action-1",
        body=action_approval.ActionApprovalDecisionRequest(
            decision=decision,
            process_id=process_id,
            tool_request_id=request_id,
            approval_session_id=session_id,
        ),
        resolved_user_id="user-1",
    )


@pytest.mark.asyncio
async def test_child_decision_settles_only_its_own_blocker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, child_a, child_b, session_a, request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)
    monkeypatch.setattr(
        action_approval,
        "apply_action_approval_decision",
        lambda _command: pytest.fail("root decision writer must not be called"),
    )

    response = await _decide(child_a["process_id"], session_a, request_a)

    assert response.process_id == child_a["process_id"]
    assert response.approval_session_id == session_a
    assert response.accepted is True
    assert _session_statuses(db_path) == {
        session_a: "approved_once",
        "session-b": "pending",
        ROOT_SESSION_ID: "pending",
    }
    assert snapshot._waiting(db_path)[-1] == [
        (snapshot.ROOT_PROCESS_ID, ROOT_REQUEST_ID),
        (child_b["process_id"], "request-b"),
    ]


@pytest.mark.asyncio
async def test_resent_child_decision_converges_as_already_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, child_a, _child_b, session_a, request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)
    await _decide(child_a["process_id"], session_a, request_a)
    settled_statuses = _session_statuses(db_path)
    settled_snapshots = snapshot._waiting(db_path)

    with pytest.raises(HTTPException) as resent:
        await _decide(child_a["process_id"], session_a, request_a)
    with pytest.raises(HTTPException) as reversed_decision:
        await _decide(child_a["process_id"], session_a, request_a, decision="denied")

    assert resent.value.status_code == status.HTTP_409_CONFLICT
    assert resent.value.detail["error_code"] == "APPROVAL_DECISION_ALREADY_APPLIED"
    assert reversed_decision.value.detail["error_code"] == "APPROVAL_DECISION_CONFLICT"
    assert _session_statuses(db_path) == settled_statuses
    assert snapshot._waiting(db_path) == settled_snapshots


def _repause_child(db_path: Path, child_process_id: str, *, name: str) -> None:
    """Run the resumed child up to its next gated Tool call.

    A child worker claims only while its exact root job runs, so the scenery
    root returns to the running shape its own approval decision would restore.
    """

    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET status='running' WHERE job_id=?", (snapshot.ROOT_JOB_ID,)
        )
        connection.execute(
            "UPDATE processes SET status='running',current_job_id=? WHERE process_id=?",
            (snapshot.ROOT_JOB_ID, snapshot.ROOT_PROCESS_ID),
        )
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        job_type="execute_action_subagent",
        owner_user_id="user-1",
        claimed_by="worker-a",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    payload = parse_action_subagent_job_payload_json(claimed["payload_json"])
    assert payload["process_id"] == child_process_id
    snapshot._pause_child(db_path, payload, name=name)


@pytest.mark.asyncio
async def test_resent_child_decision_is_already_applied_across_pause_generations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, child_a, _child_b, session_a, request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)
    await _decide(child_a["process_id"], session_a, request_a)
    _repause_child(db_path, child_a["process_id"], name="a2")
    settled_snapshots = snapshot._waiting(db_path)

    with pytest.raises(HTTPException) as resent:
        await _decide(child_a["process_id"], session_a, request_a)
    with pytest.raises(HTTPException) as reversed_decision:
        await _decide(child_a["process_id"], session_a, request_a, decision="denied")

    assert resent.value.detail["error_code"] == "APPROVAL_DECISION_ALREADY_APPLIED"
    assert reversed_decision.value.detail["error_code"] == "APPROVAL_DECISION_CONFLICT"
    assert snapshot._waiting(db_path) == settled_snapshots
    # The blocker the child is actually holding now still settles normally.
    response = await _decide(child_a["process_id"], "session-a2", "request-a2")
    assert response.process_id == child_a["process_id"]
    assert _session_statuses(db_path)["session-a2"] == "approved_once"


@pytest.mark.asyncio
async def test_root_decision_uses_the_root_writer_and_returns_the_root_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, _child_a, _child_b, _session_a, _request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)
    commands: list[ActionApprovalDecisionCommand] = []

    def apply(command: ActionApprovalDecisionCommand) -> ActionApprovalDecisionResult:
        commands.append(command)
        return ActionApprovalDecisionResult(
            approval_session_id=ROOT_SESSION_ID,
            process_id=snapshot.ROOT_PROCESS_ID,
            job_id=snapshot.ROOT_JOB_ID,
            decision="approved_once",
        )

    monkeypatch.setattr(action_approval, "apply_action_approval_decision", apply)
    monkeypatch.setattr(
        action_approval,
        "apply_action_subagent_approval_decision",
        lambda **_kwargs: pytest.fail("child decision writer must not be called"),
    )

    response = await _decide(snapshot.ROOT_PROCESS_ID, ROOT_SESSION_ID, ROOT_REQUEST_ID)

    assert [command.process_id for command in commands] == [snapshot.ROOT_PROCESS_ID]
    assert response.process_id == snapshot.ROOT_PROCESS_ID
    assert _session_statuses(db_path)[ROOT_SESSION_ID] == "pending"


@pytest.mark.asyncio
async def test_unknown_process_is_rejected_as_a_missing_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, _child_a, _child_b, _session_a, _request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        await _decide("ghost-process", ROOT_SESSION_ID, ROOT_REQUEST_ID)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
    assert exc_info.value.detail["error_code"] == "APPROVAL_NOT_FOUND"


@pytest.mark.asyncio
async def test_child_decision_that_misses_its_anchor_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, child_a, _child_b, _session_a, _request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        await _decide(child_a["process_id"], ROOT_SESSION_ID, ROOT_REQUEST_ID)

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT
    assert exc_info.value.detail["error_code"] == "APPROVAL_DECISION_CONFLICT"
    assert _session_statuses(db_path)[ROOT_SESSION_ID] == "pending"


@pytest.mark.asyncio
async def test_child_decision_cannot_allow_a_folder_for_the_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path, child_a, _child_b, session_a, request_a = _paused_action(tmp_path)
    _bind_runtime(monkeypatch, db_path)

    with pytest.raises(HTTPException) as exc_info:
        await _decide(
            child_a["process_id"],
            session_a,
            request_a,
            decision="approved_for_conversation",
        )

    assert exc_info.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert exc_info.value.detail["error_code"] == "APPROVAL_REQUEST_MISMATCH"
    assert _session_statuses(db_path)[session_a] == "pending"

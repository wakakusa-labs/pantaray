"""External Stop over a parent Action that is parked or waiting to be claimed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.action_approval_projection import (
    ACTION_APPROVAL_SNAPSHOT_EVENT,
)
from pantaray_agents.local_runtime.runtime.action_cancel_repository import (
    ActionCancelRepository,
    ActionCancelResult,
)
from pantaray_agents.local_runtime.runtime.action_subagent_pause import (
    ActionSubagentApprovalPause,
    pause_action_subagent_for_approval,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    build_action_subagent_canceled_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    mark_local_action_job_paused,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.tasks.action_user_message import (
    render_action_user_request_text,
    serialize_action_user_message,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from . import test_action_subagent_spawn as spawn

ROOT_PROCESS_ID = "parent-process"
ROOT_JOB_ID = "parent-job"
STOP_AT = "2026-09-01T01:00:00Z"


def _stop_ready_runtime(tmp_path: Path) -> tuple[Path, ActionExecutionContext]:
    """Build the spawn fixture with the durable USER turn an external Stop reads."""

    db_path, context = spawn._runtime(tmp_path)
    message = ActionUserMessageInput(message_id="message-user-1", content="Start work")
    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE agent_actions SET suggestion_id=NULL WHERE action_id='action-1'"
        )
        connection.execute(
            "UPDATE agent_action_steps SET user_message_id=?,user_message_json=?,"
            "user_request_text=? WHERE step_id='root-user-step'",
            (
                message.message_id,
                serialize_action_user_message(message),
                render_action_user_request_text(message),
            ),
        )
    return db_path, context


def _spawn_child(
    db_path: Path, context: ActionExecutionContext, *, index: int
) -> ActionSubagentJobPayload:
    """Spawn the ``index``-th child; the fixture already owns the first THINK."""

    request = spawn._request(context)
    if index > 1:
        request = spawn._request(
            context,
            llm_step_id=f"supervisor-think-{index}",
            claim_key=f"repository:child-{index}",
        )
        spawn._insert_think(db_path, request, step_number=index + 1)
    spawned = spawn._spawn(db_path, request)
    with spawn._connect(db_path) as connection:
        payload_json = connection.execute(
            "SELECT payload_json FROM job_payloads WHERE job_id=?", (spawned.job_id,)
        ).fetchone()[0]
    return parse_action_subagent_job_payload_json(str(payload_json))


def _claim_child(db_path: Path, *, worker: str) -> ActionSubagentJobPayload:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        job_type="execute_action_subagent",
        owner_user_id="user-1",
        claimed_by=worker,
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    return parse_action_subagent_job_payload_json(claimed["payload_json"])


def _insert_pending_session(db_path: Path, *, session_id: str, request_id: str) -> None:
    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "INSERT INTO approval_sessions(approval_session_id,user_id,action_id,"
            "tool_request_id,tool_id,intent_class,approval_source,status,"
            "approved_capabilities_json,command_summary_json,requested_at,created_at)"
            " VALUES (?,'user-1','action-1',?,'apply_patch','write','prompt',"
            "'pending','{}',?,?,?)",
            (
                session_id,
                request_id,
                json.dumps({"summary": request_id}),
                spawn.TIMESTAMP,
                spawn.TIMESTAMP,
            ),
        )


def _pause_child(db_path: Path, payload: ActionSubagentJobPayload) -> str:
    session_id = f"session-{payload['process_id']}"
    request_id = f"request-{payload['process_id']}"
    _insert_pending_session(db_path, session_id=session_id, request_id=request_id)
    assert (
        pause_action_subagent_for_approval(
            db_path=db_path,
            busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
            payload=payload,
            pause=ActionSubagentApprovalPause(
                tool_id="apply_patch",
                call_id="call-1",
                arguments={"path": "src"},
                tool_request_id=request_id,
                approval_session_id=session_id,
                intent_class="write",
                command_summary={"summary": request_id},
            ),
        )
        is None
    )
    return session_id


def _park_parent(db_path: Path) -> str:
    """Park the root on its own approval anchor, exactly as its worker would."""

    _insert_pending_session(
        db_path, session_id="session-root", request_id="request-root"
    )
    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "INSERT INTO job_attempts(attempt_id,job_id,attempt_number,started_at,"
            "status) VALUES ('parent-attempt',?,1,?,'running')",
            (ROOT_JOB_ID, spawn.TIMESTAMP),
        )
    assert (
        mark_local_action_job_paused(
            db_path=db_path,
            busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
            job_id=ROOT_JOB_ID,
            process_id=ROOT_PROCESS_ID,
            payload={
                "action_id": "action-1",
                "user_id": "user-1",
                "status": "processing",
                "completed_at": spawn.TIMESTAMP,
                "reason": "approval_pending",
                "approval_blockers": [
                    {
                        "approval_session_id": "session-root",
                        "tool_request_id": "request-root",
                    }
                ],
            },
        )
        is not None
    )
    return "session-root"


def _requeue_parent(db_path: Path) -> None:
    """Reproduce the generic in-flight repair that re-queues an interrupted root."""

    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET status='queued',claimed_by=NULL,claimed_at=NULL,"
            "heartbeat_at=NULL WHERE job_id=?",
            (ROOT_JOB_ID,),
        )
        connection.execute(
            "UPDATE processes SET status='enqueued',current_job_id=NULL "
            "WHERE process_id=?",
            (ROOT_PROCESS_ID,),
        )


def _stop(db_path: Path, *, completed_at: str = STOP_AT) -> ActionCancelResult:
    return asyncio.run(
        ActionCancelRepository(
            db_path=db_path, busy_timeout_ms=spawn.BUSY_TIMEOUT_MS
        ).cancel_processing_action(
            user_id="user-1",
            action_id="action-1",
            completed_at=completed_at,
            process_completed_event_id=f"stop-{completed_at}",
        )
    )


def _parent_state(db_path: Path) -> tuple[object, ...]:
    with spawn._connect(db_path) as connection:
        row = connection.execute(
            "SELECT jobs.status,jobs.cancel_requested_at IS NOT NULL,"
            "processes.status,agent_actions.status FROM jobs "
            "JOIN processes ON processes.process_id=jobs.process_id "
            "JOIN agent_actions ON agent_actions.action_id=processes.action_id "
            "WHERE jobs.job_id=?",
            (ROOT_JOB_ID,),
        ).fetchone()
    return tuple(row)


def _child_state(
    db_path: Path, payload: ActionSubagentJobPayload
) -> tuple[object, ...]:
    with spawn._connect(db_path) as connection:
        row = connection.execute(
            "SELECT jobs.status,jobs.cancel_requested_at IS NOT NULL,"
            "processes.status,(SELECT COUNT(*) FROM action_subagent_resource_claims "
            "WHERE child_process_id=processes.process_id AND released_at IS NULL) "
            "FROM jobs JOIN processes ON processes.process_id=jobs.process_id "
            "WHERE jobs.job_id=?",
            (payload["job_id"],),
        ).fetchone()
    return tuple(row)


def _approval_statuses(db_path: Path) -> dict[str, str]:
    with spawn._connect(db_path) as connection:
        return {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT approval_session_id,status FROM approval_sessions"
            ).fetchall()
        }


def _snapshot_blockers(db_path: Path) -> list[list[str]]:
    with spawn._connect(db_path) as connection:
        return [
            [
                str(blocker["tool_request_id"])
                for blocker in json.loads(str(row[0]))["approval_blockers"]
            ]
            for row in connection.execute(
                "SELECT payload_json FROM process_events WHERE process_id=? "
                "AND event_name=? ORDER BY event_seq",
                (ROOT_PROCESS_ID, ACTION_APPROVAL_SNAPSHOT_EVENT),
            ).fetchall()
        ]


@pytest.mark.parametrize("park", [True, False])
def test_stop_settles_children_of_a_parent_without_a_live_worker(
    tmp_path: Path, park: bool
) -> None:
    db_path, context = _stop_ready_runtime(tmp_path)
    paused_child = _spawn_child(db_path, context, index=1)
    claimed = _claim_child(db_path, worker="worker-a")
    assert claimed["process_id"] == paused_child["process_id"]
    paused_session = _pause_child(db_path, claimed)
    queued_child = _spawn_child(db_path, context, index=2)
    root_session = _park_parent(db_path) if park else None
    if not park:
        _requeue_parent(db_path)

    result = _stop(db_path)

    assert (result.changed, result.action_status) == (True, "canceled")
    assert _parent_state(db_path) == ("canceled", 1, "canceled", "canceled")
    assert _child_state(db_path, queued_child) == ("canceled", 1, "canceled", 0)
    assert _child_state(db_path, paused_child) == ("canceled", 1, "canceled", 0)
    statuses = _approval_statuses(db_path)
    assert statuses[paused_session] == "interrupted"
    if root_session is not None:
        assert statuses[root_session] == "interrupted"
    # Releasing the paused child's own blocker republishes the whole waiting set.
    assert _snapshot_blockers(db_path)[-1] == (["request-root"] if park else [])


@pytest.mark.parametrize("park", [True, False])
def test_stop_fences_a_parent_without_a_worker_while_a_child_runs(
    tmp_path: Path, park: bool
) -> None:
    db_path, context = _stop_ready_runtime(tmp_path)
    running_child = _spawn_child(db_path, context, index=1)
    assert _claim_child(db_path, worker="worker-a") == running_child
    queued_child = _spawn_child(db_path, context, index=2)
    root_session = _park_parent(db_path) if park else None
    if not park:
        _requeue_parent(db_path)

    result = _stop(db_path)

    assert (result.changed, result.action_status) == (True, None)
    assert _parent_state(db_path) == (
        ("paused", 1, "paused", "processing")
        if park
        else ("queued", 1, "enqueued", "processing")
    )
    # A live child keeps its claim until its own worker reaches a safe boundary.
    assert _child_state(db_path, running_child) == ("running", 1, "running", 1)
    assert _child_state(db_path, queued_child) == ("canceled", 1, "canceled", 0)
    if root_session is not None:
        # Nothing may answer for the user, so the parked approval survives the
        # fence and still offers its own convergence path.
        assert _approval_statuses(db_path)[root_session] == "pending"

    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        payload=running_child,
        result=build_action_subagent_canceled_result(),
    )
    replayed = _stop(db_path, completed_at="2026-09-01T02:00:00Z")

    assert (replayed.changed, replayed.action_status) == (True, "canceled")
    assert _parent_state(db_path) == ("canceled", 1, "canceled", "canceled")
    assert _child_state(db_path, running_child) == ("canceled", 1, "canceled", 0)
    if root_session is not None:
        assert _approval_statuses(db_path)[root_session] == "interrupted"


def test_stop_terminalizes_a_parked_parent_that_owns_no_child(tmp_path: Path) -> None:
    db_path, _context = _stop_ready_runtime(tmp_path)
    root_session = _park_parent(db_path)

    result = _stop(db_path)

    assert (result.changed, result.action_status) == (True, "canceled")
    assert _parent_state(db_path) == ("canceled", 1, "canceled", "canceled")
    assert _approval_statuses(db_path)[root_session] == "interrupted"

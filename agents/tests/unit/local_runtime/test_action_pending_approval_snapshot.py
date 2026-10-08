from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pantaray_agents.local_runtime.runtime.action_approval_projection import (
    ACTION_APPROVAL_SNAPSHOT_EVENT,
    append_pending_approval_snapshot_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_subagent_approval import (
    apply_action_subagent_approval_decision,
)
from pantaray_agents.local_runtime.runtime.action_subagent_cancel import (
    ActionSubagentCancelRequest,
    request_action_subagent_cancellation,
)
from pantaray_agents.local_runtime.runtime.action_subagent_pause import (
    ActionSubagentApprovalPause,
    pause_action_subagent_for_approval,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    mark_local_action_job_paused,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from . import test_action_subagent_spawn as spawn

ROOT_PROCESS_ID = "parent-process"
ROOT_JOB_ID = "parent-job"


def _running_children(
    tmp_path: Path,
) -> tuple[Path, ActionSubagentJobPayload, ActionSubagentJobPayload]:
    db_path, context = spawn._runtime(tmp_path)
    spawn._spawn(db_path, spawn._request(context))
    second = spawn._request(
        context,
        llm_step_id="supervisor-think-2",
        claim_key="repository:docs",
    )
    spawn._insert_think(db_path, second, step_number=3)
    spawn._spawn(db_path, second)
    payloads: list[ActionSubagentJobPayload] = []
    for worker in ("worker-a", "worker-b"):
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
        payloads.append(parse_action_subagent_job_payload_json(claimed["payload_json"]))
    return db_path, payloads[0], payloads[1]


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


def _pause_child(
    db_path: Path, payload: ActionSubagentJobPayload, *, name: str
) -> tuple[str, str]:
    session_id, request_id = f"session-{name}", f"request-{name}"
    _insert_pending_session(db_path, session_id=session_id, request_id=request_id)
    assert (
        pause_action_subagent_for_approval(
            db_path=db_path,
            busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
            payload=payload,
            pause=ActionSubagentApprovalPause(
                tool_id="apply_patch",
                call_id="call-1",
                arguments={"path": name},
                tool_request_id=request_id,
                approval_session_id=session_id,
                intent_class="write",
                command_summary={"summary": request_id},
            ),
        )
        is None
    )
    return session_id, request_id


def _cancel_child(db_path: Path, payload: ActionSubagentJobPayload) -> None:
    request_action_subagent_cancellation(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        request=ActionSubagentCancelRequest(
            user_id=payload["user_id"],
            action_id=payload["action_id"],
            parent_process_id=payload["parent_process_id"],
            parent_job_id=ROOT_JOB_ID,
            child_process_id=payload["process_id"],
        ),
    )


def _root_events(db_path: Path) -> list[tuple[str, str]]:
    with spawn._connect(db_path) as connection:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT event_name,payload_json FROM process_events "
                "WHERE process_id=? ORDER BY event_seq",
                (ROOT_PROCESS_ID,),
            ).fetchall()
        ]


def _snapshot_blockers(db_path: Path) -> list[list[dict[str, Any]]]:
    return [
        json.loads(payload)["approval_blockers"]
        for name, payload in _root_events(db_path)
        if name == ACTION_APPROVAL_SNAPSHOT_EVENT
    ]


def _waiting(db_path: Path) -> list[list[tuple[str, str]]]:
    return [
        [
            (str(blocker["process_id"]), str(blocker["tool_request_id"]))
            for blocker in snapshot
        ]
        for snapshot in _snapshot_blockers(db_path)
    ]


def test_child_pause_decision_and_cancel_are_ordered_on_the_root_stream(
    tmp_path: Path,
) -> None:
    db_path, child_a, child_b = _running_children(tmp_path)
    session_a, request_a = _pause_child(db_path, child_a, name="a")
    _, request_b = _pause_child(db_path, child_b, name="b")
    apply_action_subagent_approval_decision(
        db_path=db_path,
        busy_timeout_ms=spawn.BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id="action-1",
        child_process_id=child_a["process_id"],
        approval_session_id=session_a,
        tool_request_id=request_a,
        decision="approved_once",
    )
    _cancel_child(db_path, child_b)

    blocker_a = (child_a["process_id"], request_a)
    blocker_b = (child_b["process_id"], request_b)
    assert _waiting(db_path) == [
        [blocker_a],
        sorted([blocker_a, blocker_b]),
        [blocker_b],
        [],
    ]
    assert _snapshot_blockers(db_path)[0][0] == {
        "process_id": child_a["process_id"],
        "action_id": "action-1",
        "approval_session_id": session_a,
        "tool_request_id": request_a,
        "tool_id": "apply_patch",
        "intent_class": "write",
        "command_summary": {"summary": request_a},
    }


def test_root_pause_snapshot_precedes_its_own_pause_anchor(tmp_path: Path) -> None:
    db_path, child_a, _child_b = _running_children(tmp_path)
    _, request_a = _pause_child(db_path, child_a, name="a")
    _insert_pending_session(
        db_path, session_id="session-root", request_id="request-root"
    )
    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "INSERT INTO job_attempts(attempt_id,job_id,attempt_number,started_at,"
            "status) VALUES ('parent-attempt',?,1,?,'running')",
            (ROOT_JOB_ID, spawn.TIMESTAMP),
        )
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

    assert [name for name, _ in _root_events(db_path)][-2:] == [
        ACTION_APPROVAL_SNAPSHOT_EVENT,
        "process_paused",
    ]
    assert _waiting(db_path)[-1] == [
        (ROOT_PROCESS_ID, "request-root"),
        (child_a["process_id"], request_a),
    ]


def test_snapshot_drops_a_paused_anchor_whose_session_was_decided(
    tmp_path: Path,
) -> None:
    db_path, child_a, _child_b = _running_children(tmp_path)
    session_a, _request_a = _pause_child(db_path, child_a, name="a")
    with spawn._connect(db_path) as connection, immediate_transaction(connection):
        connection.execute(
            "UPDATE approval_sessions SET status='denied',decided_at=? "
            "WHERE approval_session_id=?",
            (spawn.TIMESTAMP, session_a),
        )
        append_pending_approval_snapshot_in_connection(
            connection,
            user_id="user-1",
            action_id="action-1",
            root_process_id=ROOT_PROCESS_ID,
            created_at=spawn.TIMESTAMP,
        )

    assert _waiting(db_path) == [[(child_a["process_id"], _request_a)], []]

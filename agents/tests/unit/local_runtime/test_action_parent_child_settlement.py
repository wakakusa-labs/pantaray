from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.action_status import (
    ACTION_FAILURE_CODE_CANCELED,
    FinalizeActionTerminalCommand,
    build_finalize_action_terminal_command,
    derive_action_terminal_failure,
)
from pantaray_agents.local_runtime.runtime.action_approval_projection import (
    ACTION_APPROVAL_SNAPSHOT_EVENT,
)
from pantaray_agents.local_runtime.runtime.action_subagent_parent_lifecycle import (
    ActionChildSettlementPendingError,
)
from pantaray_agents.local_runtime.runtime.action_subagent_pause import (
    ActionSubagentApprovalPause,
    pause_action_subagent_for_approval,
)
from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_subagent_spawn import (
    ActionSubagentSpawnAuthorityError,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    build_action_subagent_success_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.local_runtime.runtime.action_terminal_repository import (
    ActionTerminalRepository,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_envelope import (
    LocalJobEnvelopeIntegrityError,
)
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.suggestion_state.action_terminal_projection import (
    ActionTerminalProjectionResult,
)
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    ExternalResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from . import test_action_subagent_spawn as spawn
from .test_action_terminal_repository import (
    BUSY_TIMEOUT_MS,
    _action_memory_draft_json,
    _bootstrap_db,
    _enqueue_and_claim_action,
)

PARENT_PROCESS_ID = "process-1"
PARENT_JOB_ID = "job-1"
SEEDED_AT = "2026-03-24T00:02:00Z"
COMPLETED_AT = "2026-03-24T00:03:00Z"


def _parent_runtime(tmp_path: Path) -> Path:
    db_path = _bootstrap_db(tmp_path)
    _enqueue_and_claim_action(db_path)
    return db_path


def _seed_child(db_path: Path, *, ordinal: int = 1) -> ActionSubagentJobPayload:
    """Insert one enqueued child with an active claim under the parent."""

    claim_id = f"claim-{ordinal}"
    payload = build_action_subagent_job_payload(
        {
            "job_id": f"child-job-{ordinal}",
            "process_id": f"child-process-{ordinal}",
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": PARENT_PROCESS_ID,
            "inference_profile_id": "action.subagent.luna",
            "action_context": "# Workspace Paths\nparent context",
            "task": f"inspect boundary {ordinal}",
            "context_refs": [],
            "resource_claim_ids": [claim_id],
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            enqueue_action_subagent_job_in_connection(
                connection=connection,
                payload=payload,
                scheduled_at=SEEDED_AT,
            )
            acquire_action_subagent_resource_claims_in_connection(
                connection,
                user_id="user-1",
                action_id="action-1",
                parent_process_id=PARENT_PROCESS_ID,
                child_process_id=payload["process_id"],
                acquired_at=SEEDED_AT,
                claims=(
                    ExternalResourceClaim(
                        claim_id=claim_id,
                        root_identity="workspace",
                        normalized_key=f"repository:{ordinal}",
                    ),
                ),
            )
    return payload


def _claim_child(db_path: Path) -> None:
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_type="execute_action_subagent",
            owner_user_id="user-1",
            claimed_by="child-worker",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )


def _pause_child(db_path: Path, payload: ActionSubagentJobPayload) -> str:
    session_id = f"session-{payload['process_id']}"
    request_id = f"request-{payload['process_id']}"
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "INSERT INTO approval_sessions(approval_session_id,user_id,action_id,"
            "tool_request_id,tool_id,intent_class,approval_source,status,"
            "approved_capabilities_json,command_summary_json,requested_at,created_at)"
            " VALUES (?,'user-1','action-1',?,'apply_patch','write','prompt',"
            "'pending','{}',?,?,?)",
            (session_id, request_id, json.dumps({}), SEEDED_AT, SEEDED_AT),
        )
    assert (
        pause_action_subagent_for_approval(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            payload=payload,
            pause=ActionSubagentApprovalPause(
                tool_id="apply_patch",
                arguments={"path": "a"},
                tool_request_id=request_id,
                approval_session_id=session_id,
                intent_class="write",
                command_summary={},
            ),
        )
        is None
    )
    return session_id


def _error_command(*, event_id: str = "event-1") -> FinalizeActionTerminalCommand:
    return build_finalize_action_terminal_command(
        process_completed_event_id=event_id,
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id=PARENT_PROCESS_ID,
        action_id="action-1",
        completed_at=COMPLETED_AT,
        action_status="error",
        failure=derive_action_terminal_failure("error"),
    )


def _success_command(db_path: Path) -> FinalizeActionTerminalCommand:
    return build_finalize_action_terminal_command(
        process_completed_event_id="event-1",
        user_id="user-1",
        suggestion_id="sug-1",
        accepted_at="2026-03-24T00:00:00Z",
        command_id="command-1",
        process_id=PARENT_PROCESS_ID,
        action_id="action-1",
        completed_at=COMPLETED_AT,
        action_status="success",
        final_output="done",
        memory_draft_json=_action_memory_draft_json(db_path),
    )


def _fence_parent(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET cancel_requested_at = ? WHERE job_id = ?",
            (SEEDED_AT, PARENT_JOB_ID),
        )


def _stream_end_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM process_events"
                " WHERE process_id = ? AND event_name = 'stream_end'",
                (PARENT_PROCESS_ID,),
            ).fetchone()[0]
        )


async def _finalize_parent(
    db_path: Path, command: FinalizeActionTerminalCommand
) -> ActionTerminalProjectionResult:
    return await ActionTerminalRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).finalize_action_job_terminal(
        command=command,
        job_id=PARENT_JOB_ID,
        runtime_state_checkpoint=None,
    )


def _parent_state(db_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """
                SELECT process.status, process.terminal_event_id IS NOT NULL,
                       job.status, action.status
                FROM processes AS process
                JOIN jobs AS job ON job.job_id = ?
                JOIN agent_actions AS action ON action.action_id = process.action_id
                WHERE process.process_id = ?
                """,
                (PARENT_JOB_ID, PARENT_PROCESS_ID),
            ).fetchone()
        )


def _child_state(
    db_path: Path, payload: ActionSubagentJobPayload
) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """
                SELECT job.status, process.status,
                       job.cancel_requested_at IS NOT NULL,
                       (SELECT COUNT(*) FROM action_subagent_resource_claims
                        WHERE child_process_id = process.process_id
                          AND released_at IS NULL)
                FROM jobs AS job
                JOIN processes AS process ON process.process_id = job.process_id
                WHERE job.job_id = ?
                """,
                (payload["job_id"],),
            ).fetchone()
        )


def _root_event_names(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event_name FROM process_events WHERE process_id = ?"
                " ORDER BY event_seq",
                (PARENT_PROCESS_ID,),
            ).fetchall()
        ]


@pytest.mark.asyncio
async def test_running_child_holds_the_parent_terminal_until_it_settles(
    tmp_path: Path,
) -> None:
    db_path = _parent_runtime(tmp_path)
    child = _seed_child(db_path)
    _claim_child(db_path)

    with pytest.raises(ActionChildSettlementPendingError):
        await _finalize_parent(db_path, _error_command())

    assert _parent_state(db_path) == ("running", 0, "running", "processing")
    assert _root_event_names(db_path) == []
    # The running child keeps its claim but carries the durable cancel request.
    assert _child_state(db_path, child) == ("running", "running", 1, 1)

    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        payload=child,
        result=build_action_subagent_success_result("late report"),
    )
    assert _child_state(db_path, child) == ("canceled", "canceled", 1, 0)

    result = await _finalize_parent(db_path, _error_command())

    assert result.action_status == "error"
    assert _parent_state(db_path) == ("failed", 1, "failed", "error")


@pytest.mark.asyncio
async def test_orphan_child_claim_fails_the_parent_terminal_loudly(
    tmp_path: Path,
) -> None:
    """A claim without a nonterminal child is unreachable, so it must not wait.

    Today's external Stop transaction cancels child processes without settling
    their jobs or releasing their claims. Nothing can settle such a claim later,
    so waiting for it would occupy the worker forever.
    """

    db_path = _parent_runtime(tmp_path)
    child = _seed_child(db_path)
    _claim_child(db_path)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE processes SET status = 'canceled' WHERE process_id = ?",
            (child["process_id"],),
        )

    with pytest.raises(LocalJobEnvelopeIntegrityError):
        await _finalize_parent(db_path, _error_command())

    assert _parent_state(db_path) == ("running", 0, "running", "processing")


@pytest.mark.asyncio
async def test_queued_and_paused_children_settle_in_the_parent_terminal(
    tmp_path: Path,
) -> None:
    db_path = _parent_runtime(tmp_path)
    queued = _seed_child(db_path, ordinal=1)
    paused = _seed_child(db_path, ordinal=2)
    _claim_child(db_path)
    _claim_child(db_path)
    session_id = _pause_child(db_path, paused)
    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        payload=queued,
        result=build_action_subagent_success_result("first report"),
    )
    requeued = _seed_child(db_path, ordinal=3)

    result = await _finalize_parent(db_path, _error_command())

    assert result.action_status == "error"
    assert _child_state(db_path, paused) == ("canceled", "canceled", 1, 0)
    assert _child_state(db_path, requeued) == ("canceled", "canceled", 1, 0)
    assert _parent_state(db_path) == ("failed", 1, "failed", "error")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id = ?",
            (session_id,),
        ).fetchone() == ("interrupted",)
    # The paused child released its blocker inside the parent terminal
    # transaction, so the cancel core wrote the replacement snapshot before the
    # terminal event.
    assert _root_event_names(db_path) == [
        ACTION_APPROVAL_SNAPSHOT_EVENT,
        ACTION_APPROVAL_SNAPSHOT_EVENT,
        "stream_end",
    ]
    with sqlite3.connect(db_path) as connection:
        snapshots = [
            json.loads(str(row[0]))["approval_blockers"]
            for row in connection.execute(
                "SELECT payload_json FROM process_events WHERE process_id = ?"
                " AND event_name = ? ORDER BY event_seq",
                (PARENT_PROCESS_ID, ACTION_APPROVAL_SNAPSHOT_EVENT),
            ).fetchall()
        ]
    assert [len(snapshot) for snapshot in snapshots] == [1, 0]


@pytest.mark.asyncio
async def test_parent_cancel_fence_wins_a_late_worker_success(tmp_path: Path) -> None:
    db_path = _parent_runtime(tmp_path)
    _fence_parent(db_path)
    command = _success_command(db_path)

    result = await _finalize_parent(db_path, command)

    assert (result.action_status, result.action_failure_code) == (
        "canceled",
        ACTION_FAILURE_CODE_CANCELED,
    )
    assert _parent_state(db_path) == ("canceled", 1, "canceled", "canceled")
    with sqlite3.connect(db_path) as connection:
        payload = json.loads(
            str(
                connection.execute(
                    "SELECT payload_json FROM process_events"
                    " WHERE process_id = ? AND event_name = 'stream_end'",
                    (PARENT_PROCESS_ID,),
                ).fetchone()[0]
            )
        )
    assert payload["status"] == "canceled"
    assert "final_output" not in payload


@pytest.mark.asyncio
async def test_fenced_parent_terminal_replays_the_canceled_winner(
    tmp_path: Path,
) -> None:
    """Re-sending the fenced event must read back the persisted cancel winner."""

    db_path = _parent_runtime(tmp_path)
    _fence_parent(db_path)
    command = _success_command(db_path)
    first = await _finalize_parent(db_path, command)

    replayed = await _finalize_parent(db_path, command)

    assert (replayed.action_status, replayed.action_failure_code) == (
        "canceled",
        ACTION_FAILURE_CODE_CANCELED,
    )
    assert replayed.final_output is None
    assert replayed.process_completed_sequence == first.process_completed_sequence
    assert _stream_end_count(db_path) == 1


@pytest.mark.asyncio
async def test_unfenced_parent_terminal_replays_its_own_winner(
    tmp_path: Path,
) -> None:
    db_path = _parent_runtime(tmp_path)
    command = _error_command()
    first = await _finalize_parent(db_path, command)

    replayed = await _finalize_parent(db_path, command)

    assert (replayed.action_status, replayed.action_failure_code) == (
        "error",
        first.action_failure_code,
    )
    assert replayed.process_completed_sequence == first.process_completed_sequence
    assert _stream_end_count(db_path) == 1


def test_spawn_is_refused_after_the_parent_cancel_fence(tmp_path: Path) -> None:
    db_path, context = spawn._runtime(tmp_path)
    request = spawn._request(context)
    spawn._spawn(db_path, request)
    replayed = spawn._request(context, llm_step_id="supervisor-think-2")
    spawn._insert_think(db_path, replayed, step_number=3)
    with spawn._connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET cancel_requested_at = ? WHERE job_id = 'parent-job'",
            (spawn.TIMESTAMP,),
        )

    for fenced in (request, replayed):
        with pytest.raises(ActionSubagentSpawnAuthorityError, match="not active"):
            spawn._spawn(db_path, fenced)

    with spawn._connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM processes WHERE kind = 'action_subagent'"
            ).fetchone()[0]
            == 1
        )

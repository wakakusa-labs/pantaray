from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    EXECUTION_CONTEXT_STATE_FIELDS,
)
from pantaray_agents.local_runtime.runtime.action_startup_recovery import (
    recover_interrupted_action_runs_for_startup,
)
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ActionSubagentParentMessage,
    load_action_subagent_transcript,
)
from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_subagent_startup_recovery import (
    ActionSubagentStartupRecovery,
    recover_action_subagent_children_for_startup,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    build_action_subagent_success_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.local_runtime.runtime.job_claim import (
    claim_next_pending_job,
    peek_next_pending_job_type,
)
from pantaray_agents.local_runtime.runtime.job_envelope import (
    LocalJobEnvelopeIntegrityError,
)
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    mark_local_action_job_paused,
)
from pantaray_agents.local_runtime.storage.migrations import (
    repair_inflight_jobs_for_startup,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    ExternalResourceClaim,
    acquire_action_subagent_resource_claims_in_connection,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ToolInvocationCompletionInput,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from . import test_action_pending_approval_snapshot as snapshot
from . import test_action_startup_recovery as parent_recovery
from .resource_recovery_test_support import register_running_apply_patch_invocation

BUSY_TIMEOUT_MS = 1_000
TIMESTAMP = parent_recovery.TIMESTAMP
ROOT = ("user-1", "action-1")
PARENT_PROCESS_ID = "process-1"
PARENT_JOB_ID = "job-1"
CHILD_PROCESS_ID = "child-process-1"
CHILD_JOB_ID = "child-job-1"
APPROVAL_SESSION_ID = "approval-child-1"
# A child Tool request id carries the process event sequence it was claimed at.
TOOL_REQUEST_ID = f"{CHILD_PROCESS_ID}:1"
INVOCATION_ID = "invocation-apply-patch"
PAUSE_PAYLOAD: dict[str, object] = {
    "action_id": "action-1",
    "user_id": "user-1",
    "status": "processing",
    "reason": "approval_pending",
    "approval_blockers": [
        {"approval_session_id": APPROVAL_SESSION_ID, "tool_request_id": TOOL_REQUEST_ID}
    ],
}


def _seed_child(db_path: Path) -> ActionSubagentJobPayload:
    payload = build_action_subagent_job_payload(
        {
            "job_id": CHILD_JOB_ID,
            "process_id": CHILD_PROCESS_ID,
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": PARENT_PROCESS_ID,
            "inference_profile_id": "action.subagent.luna",
            "action_context": "# Workspace Paths\nparent context",
            "task": "inspect the restart boundary",
            "context_refs": [],
            "resource_claim_ids": ["claim-1"],
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            enqueue_action_subagent_job_in_connection(
                connection=connection, payload=payload, scheduled_at=TIMESTAMP
            )
            acquire_action_subagent_resource_claims_in_connection(
                connection,
                user_id="user-1",
                action_id="action-1",
                parent_process_id=PARENT_PROCESS_ID,
                child_process_id=CHILD_PROCESS_ID,
                acquired_at=TIMESTAMP,
                claims=(
                    ExternalResourceClaim(
                        claim_id="claim-1", root_identity="ws", normalized_key="repo"
                    ),
                ),
            )
    return payload


def _claim(db_path: Path, job_type: str) -> bool:
    return (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_type=job_type,
            owner_user_id="user-1",
            claimed_by="worker-1",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )


def _restart_scenario(
    tmp_path: Path,
) -> tuple[Path, ActionExecutionContext, ActionSubagentJobPayload]:
    """Leave the DB in the exact shape a crash during a live child leaves it."""

    db_path, context = parent_recovery._seed_interrupted_run(tmp_path)
    parent_recovery._insert_checkpoint(db_path, context)
    payload = _seed_child(db_path)
    assert _claim(db_path, "execute_action_subagent")
    return db_path, context, payload


def _recover_children(db_path: Path) -> ActionSubagentStartupRecovery:
    return recover_action_subagent_children_for_startup(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    )


def _recover_parent(db_path: Path, preserved: frozenset[tuple[str, str]]) -> int:
    return recover_interrupted_action_runs_for_startup(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, preserved_roots=preserved
    )


def _update(db_path: Path, sql: str, params: tuple[object, ...]) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(sql, params)


def _fence_parent(db_path: Path) -> None:
    _update(
        db_path,
        "UPDATE jobs SET cancel_requested_at=? WHERE job_id=?",
        (TIMESTAMP, PARENT_JOB_ID),
    )


def _child_state(db_path: Path) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """SELECT job.status, process.status,
                    job.cancel_requested_at IS NOT NULL,
                    (SELECT COUNT(*) FROM action_subagent_resource_claims
                     WHERE child_process_id = process.process_id
                       AND released_at IS NULL),
                    process.result_collected_at IS NULL
                   FROM jobs AS job JOIN processes AS process
                     ON process.process_id = job.process_id
                   WHERE job.job_id = ?""",
                (CHILD_JOB_ID,),
            ).fetchone()
        )


def _root_state(db_path: Path, context: ActionExecutionContext) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                """SELECT (SELECT status FROM execution_sessions
                    WHERE execution_session_id = ?), (SELECT status
                    FROM tool_runtime_resources WHERE action_id = 'action-1'
                      AND tool_invocation_id IS NULL)""",
                (context.execution_session_id,),
            ).fetchone()
        )


def _json_column(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> object:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(sql, params).fetchone()
    assert row is not None
    return json.loads(str(row[0]))


def _child_terminal_payload(db_path: Path) -> object:
    return _json_column(
        db_path,
        "SELECT payload_json FROM process_events"
        " WHERE process_id = ? AND event_name = 'stream_end'",
        (CHILD_PROCESS_ID,),
    )


def test_restart_preserves_a_normal_child_and_defers_it_to_its_parent(
    tmp_path: Path,
) -> None:
    db_path, context, _payload = _restart_scenario(tmp_path)

    outcome = _recover_children(db_path)

    assert outcome.preserved_roots == frozenset({ROOT})
    assert (outcome.settled_child_count, outcome.collected_invocation_count) == (0, 0)
    assert _recover_parent(db_path, outcome.preserved_roots) == 0
    # The child still owns the root, so nothing expired, deleted or stripped it.
    assert _root_state(db_path, context) == ("running", "active")
    assert context.action_temp_dir.exists()
    checkpoint = _json_column(
        db_path,
        "SELECT runtime_state_checkpoint FROM agent_action_steps"
        " WHERE step_id = 'checkpoint'",
    )
    assert isinstance(checkpoint, dict)
    assert set(EXECUTION_CONTEXT_STATE_FIELDS) <= checkpoint.keys()
    assert _child_state(db_path) == ("running", "running", 0, 1, 1)

    assert repair_inflight_jobs_for_startup(db_path, BUSY_TIMEOUT_MS) == 2
    # Parent first: the child is unclaimable until its exact parent runs again.
    assert not _claim(db_path, "execute_action_subagent")
    assert _claim(db_path, "execute_action")
    assert _claim(db_path, "execute_action_subagent")


def test_restart_settles_a_cancel_requested_child_without_requeueing_it(
    tmp_path: Path,
) -> None:
    db_path, context, _payload = _restart_scenario(tmp_path)
    _update(
        db_path,
        "UPDATE jobs SET cancel_requested_at=? WHERE job_id=?",
        (TIMESTAMP, CHILD_JOB_ID),
    )

    outcome = _recover_children(db_path)

    assert outcome.settled_child_count == 1
    assert outcome.preserved_roots == frozenset({ROOT})
    assert _child_state(db_path) == ("canceled", "canceled", 1, 0, 1)
    assert _child_terminal_payload(db_path) == {"outcome": "canceled"}
    # Only the parent job is left for the generic in-flight repair.
    assert repair_inflight_jobs_for_startup(db_path, BUSY_TIMEOUT_MS) == 1
    assert _child_state(db_path)[0] == "canceled"
    # The parent still has to read the canceled result back.
    assert _root_state(db_path, context) == ("running", "active")


def test_restart_preserves_a_terminal_uncollected_child_result(tmp_path: Path) -> None:
    db_path, context, payload = _restart_scenario(tmp_path)
    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        payload=payload,
        result=build_action_subagent_success_result("uncollected report"),
    )

    outcome = _recover_children(db_path)

    assert outcome.settled_child_count == 0
    assert outcome.preserved_roots == frozenset({ROOT})
    assert _recover_parent(db_path, outcome.preserved_roots) == 0
    assert _child_state(db_path) == ("completed", "completed", 0, 0, 1)
    assert _child_terminal_payload(db_path) == {
        "outcome": "success",
        "report": "uncollected report",
    }
    assert _root_state(db_path, context) == ("running", "active")


def test_fenced_parent_settles_its_child_and_releases_its_root(tmp_path: Path) -> None:
    db_path, context, _payload = _restart_scenario(tmp_path)
    _fence_parent(db_path)

    outcome = _recover_children(db_path)

    assert (outcome.settled_child_count, outcome.preserved_roots) == (1, frozenset())
    assert _child_state(db_path) == ("canceled", "canceled", 1, 0, 1)
    assert _recover_parent(db_path, outcome.preserved_roots) == 1
    assert _root_state(db_path, context) == ("expired", "cleaned")
    assert not context.action_temp_dir.exists()


def test_fenced_parent_never_admits_its_child_to_the_claim_boundary(
    tmp_path: Path,
) -> None:
    db_path, _context, _payload = _restart_scenario(tmp_path)
    _update(
        db_path,
        "UPDATE jobs SET status='queued',claimed_by=NULL,claimed_at=NULL"
        " WHERE job_id=?",
        (CHILD_JOB_ID,),
    )
    _update(
        db_path,
        "UPDATE processes SET status='enqueued',current_job_id=NULL WHERE process_id=?",
        (CHILD_PROCESS_ID,),
    )
    _fence_parent(db_path)

    assert (
        peek_next_pending_job_type(
            db_path=str(db_path),
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_types=("execute_action_subagent",),
            owner_user_id="user-1",
        )
        is None
    )
    assert not _claim(db_path, "execute_action_subagent")


def test_fenced_parent_pause_yields_to_its_stop_terminal(tmp_path: Path) -> None:
    """R8: parking a fenced run would strand its Stop behind an approval."""

    db_path, _context, _payload = _restart_scenario(tmp_path)
    _fence_parent(db_path)

    assert (
        mark_local_action_job_paused(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_id=PARENT_JOB_ID,
            process_id=PARENT_PROCESS_ID,
            payload=PAUSE_PAYLOAD,
        )
        is None
    )

    # Nothing parked and no anchor reached the stream, so the caller still owns
    # the run and terminalizes it as the Stop winner.
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """SELECT job.status, process.status,
                 (SELECT COUNT(*) FROM process_events WHERE process_id=?)
               FROM jobs AS job JOIN processes AS process
                 ON process.process_id = job.process_id
               WHERE job.job_id=?""",
            (PARENT_PROCESS_ID, PARENT_JOB_ID),
        ).fetchone() == ("running", "running", 0)


def test_expired_root_session_is_not_preserved(tmp_path: Path) -> None:
    """An interrupted older recovery already expired the root the child needs."""

    db_path, context, _payload = _restart_scenario(tmp_path)
    _update(
        db_path,
        "UPDATE execution_sessions SET status='expired',completed_at=?"
        " WHERE execution_session_id=?",
        (TIMESTAMP, context.execution_session_id),
    )

    outcome = _recover_children(db_path)

    assert (outcome.settled_child_count, outcome.preserved_roots) == (1, frozenset())
    assert _child_state(db_path) == ("canceled", "canceled", 1, 0, 1)
    assert _recover_parent(db_path, outcome.preserved_roots) == 1


def test_broken_child_envelope_still_reaches_the_blocked_transition(
    tmp_path: Path,
) -> None:
    """The parent gate must not hide a job the envelope check has to fail on."""

    db_path, _context, _payload = _restart_scenario(tmp_path)
    # A child job bound to a process of the wrong kind: the envelope owner has
    # to see it and block it instead of the gate leaving it queued and unseen.
    _update(
        db_path,
        "INSERT INTO jobs(job_id,user_id,job_type,process_id,status,scheduled_at,"
        "logical_key) VALUES ('broken-child','user-1','execute_action_subagent',?,"
        "'queued',?,'broken-child')",
        (PARENT_PROCESS_ID, TIMESTAMP),
    )
    _update(
        db_path,
        "INSERT INTO job_payloads(job_id,payload_json) VALUES ('broken-child','{}')",
        (),
    )

    with pytest.raises(LocalJobEnvelopeIntegrityError):
        _claim(db_path, "execute_action_subagent")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id='broken-child'"
        ).fetchone() == ("blocked", "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR")


def _claim_child_gated_tool(
    db_path: Path, context: ActionExecutionContext, *, request_id: str = TOOL_REQUEST_ID
) -> None:
    """Claim one gated child Tool the way an always_allow setting does."""

    register_running_apply_patch_invocation(db_path=db_path, context=context)
    _update(
        db_path,
        "UPDATE tool_invocations SET tool_request_id=? WHERE invocation_id=?",
        (request_id, INVOCATION_ID),
    )


@pytest.mark.parametrize(
    ("invocation_completed", "expected_transcript"),
    [(True, "Tool apply_patch completed"), (False, "StartupRecoveryInterrupted")],
)
def test_restart_reports_a_claimed_gated_invocation_instead_of_redoing_it(
    tmp_path: Path, invocation_completed: bool, expected_transcript: str
) -> None:
    """A crash between claiming the approved Tool and recording it must not redo it."""

    db_path, context, _payload = _restart_scenario(tmp_path)
    _claim_child_gated_tool(db_path, context)
    if invocation_completed:
        record_tool_invocation_completion(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            completion=ToolInvocationCompletionInput(
                invocation_id=INVOCATION_ID,
                status="completed",
                completed_at=TIMESTAMP,
                output_json={"status": "ok", "applied_paths": ["todo.txt"]},
                output_storage_kind="inline_json",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
                redaction_applied=False,
            ),
        )

    outcome = _recover_children(db_path)

    assert (outcome.collected_invocation_count, outcome.settled_child_count) == (1, 0)
    assert outcome.preserved_roots == frozenset({ROOT})
    # A second pass sees the recorded Tool event and leaves the result alone.
    assert _recover_children(db_path).collected_invocation_count == 0
    assert expected_transcript in _transcript(db_path, CHILD_PROCESS_ID)[-1]


SECOND_CHILD_JOB_ID = "child-job-2"
SECOND_CHILD_PROCESS_ID = "child-process-2"


def _seed_second_child(db_path: Path) -> None:
    """Add one more live child so a bad sibling row cannot hide its decision."""

    payload = build_action_subagent_job_payload(
        {
            "job_id": SECOND_CHILD_JOB_ID,
            "process_id": SECOND_CHILD_PROCESS_ID,
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": PARENT_PROCESS_ID,
            "inference_profile_id": "action.subagent.luna",
            "action_context": "# Workspace Paths\nparent context",
            "task": "inspect the sibling boundary",
            "context_refs": [],
            "resource_claim_ids": [],
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            enqueue_action_subagent_job_in_connection(
                connection=connection, payload=payload, scheduled_at=TIMESTAMP
            )


def _transcript(db_path: Path, process_id: str) -> tuple[str, ...]:
    """The child's durable rows as text, so a row can be matched on its content."""

    return tuple(
        f"Parent message: {entry.content}"
        if isinstance(entry, ActionSubagentParentMessage)
        else f"Tool {entry.tool_name} {entry.status}: "
        + json.dumps(entry.output, ensure_ascii=False)
        + (f" Error: {entry.error_message}" if entry.error_message else "")
        for entry in load_action_subagent_transcript(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, process_id=process_id
        )
    )


def _job_state(db_path: Path, job_id: str) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return tuple(
            connection.execute(
                "SELECT status, error_code FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        )


def test_requeued_child_still_collects_its_claimed_invocation(tmp_path: Path) -> None:
    """An older binary already re-queued this child; its result is still owed."""

    db_path, context, _payload = _restart_scenario(tmp_path)
    _claim_child_gated_tool(db_path, context)
    # The previous startup ran the generic in-flight repair without child
    # awareness, so both the parent and the child came back as queued.
    assert repair_inflight_jobs_for_startup(db_path, BUSY_TIMEOUT_MS) == 2

    outcome = _recover_children(db_path)

    assert outcome.collected_invocation_count == 1
    assert outcome.preserved_roots == frozenset({ROOT})
    assert "StartupRecoveryInterrupted" in _transcript(db_path, CHILD_PROCESS_ID)[-1]
    assert _recover_children(db_path).collected_invocation_count == 0


def test_corrupt_child_payload_blocks_only_its_own_row(tmp_path: Path) -> None:
    """One unreadable payload must not stop the pass for every other runtime."""

    db_path, _context, _payload = _restart_scenario(tmp_path)
    _seed_second_child(db_path)
    _update(
        db_path,
        'UPDATE job_payloads SET payload_json=\'{"job_id":"child-job-1"}\''
        " WHERE job_id=?",
        (CHILD_JOB_ID,),
    )

    outcome = _recover_children(db_path)

    assert outcome.blocked_child_count == 1
    assert _job_state(db_path, CHILD_JOB_ID) == (
        "blocked",
        "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR",
    )
    # The readable sibling still reached its decision, so startup continues.
    assert outcome.preserved_roots == frozenset({ROOT})
    assert _job_state(db_path, SECOND_CHILD_JOB_ID) == ("queued", None)


def test_blocked_child_job_is_not_settled_and_never_stalls_startup(
    tmp_path: Path,
) -> None:
    """A quarantined child must not roll the settle transaction into a boot loop."""

    db_path, _context, _payload = _restart_scenario(tmp_path)
    _update(
        db_path,
        "UPDATE jobs SET status='blocked',completed_at=?,"
        "error_code='LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR' WHERE job_id=?",
        (TIMESTAMP, CHILD_JOB_ID),
    )
    _fence_parent(db_path)

    outcome = _recover_children(db_path)

    assert (outcome.blocked_child_count, outcome.settled_child_count) == (1, 0)
    assert outcome.preserved_roots == frozenset()
    # The quarantine is left exactly as the envelope owner wrote it.
    assert _job_state(db_path, CHILD_JOB_ID) == (
        "blocked",
        "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR",
    )


def _corrupt_child_payload(db_path: Path) -> None:
    _update(
        db_path,
        'UPDATE job_payloads SET payload_json=\'{"job_id":"child-job-1"}\''
        " WHERE job_id=?",
        (CHILD_JOB_ID,),
    )


def test_quarantined_child_still_preserves_its_parent_root(tmp_path: Path) -> None:
    """A blocked child keeps owning the root, so recovery must not delete it."""

    db_path, context, _payload = _restart_scenario(tmp_path)
    _corrupt_child_payload(db_path)

    outcome = _recover_children(db_path)

    assert outcome.blocked_child_count == 1
    assert outcome.preserved_roots == frozenset({ROOT})
    assert _recover_parent(db_path, outcome.preserved_roots) == 0
    assert _root_state(db_path, context) == ("running", "active")
    assert context.action_temp_dir.exists()
    assert _job_state(db_path, CHILD_JOB_ID) == (
        "blocked",
        "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR",
    )
    # No pause anchor, so quarantine leaves the approval stream untouched.
    assert _root_blocker_counts(db_path) == []


def test_unblockable_terminal_child_is_not_counted_as_blocked(tmp_path: Path) -> None:
    """A terminal job cannot be blocked, so the pass must not claim it did."""

    db_path, _context, payload = _restart_scenario(tmp_path)
    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        payload=payload,
        result=build_action_subagent_success_result("uncollected report"),
    )
    _corrupt_child_payload(db_path)

    outcome = _recover_children(db_path)

    assert (outcome.blocked_child_count, outcome.unreadable_child_count) == (0, 1)
    # The durable result is still owed to the parent, so the root stays.
    assert outcome.preserved_roots == frozenset({ROOT})
    assert _job_state(db_path, CHILD_JOB_ID) == ("completed", None)


def _root_blocker_counts(db_path: Path) -> list[int]:
    with sqlite3.connect(db_path) as connection:
        return [
            len(json.loads(str(row[0]))["approval_blockers"])
            for row in connection.execute(
                "SELECT payload_json FROM process_events WHERE process_id=?"
                " AND event_name='action_approval_snapshot' ORDER BY event_seq",
                (PARENT_PROCESS_ID,),
            ).fetchall()
        ]


def test_quarantining_a_paused_child_releases_its_approval(tmp_path: Path) -> None:
    """A blocked child must not hold a decision nobody can ever answer."""

    db_path, _context, payload = _restart_scenario(tmp_path)
    session_id, _request_id = snapshot._pause_child(db_path, payload, name="q")
    assert _root_blocker_counts(db_path) == [1]
    _corrupt_child_payload(db_path)

    assert _recover_children(db_path).blocked_child_count == 1

    with sqlite3.connect(db_path) as connection:
        status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id=?",
            (session_id,),
        ).fetchone()
    assert status == ("interrupted",)
    # The root stream carries the replacement set, so the blocker stops showing.
    assert _root_blocker_counts(db_path) == [1, 0]


def test_payload_naming_another_child_never_writes_that_child(tmp_path: Path) -> None:
    """A parseable payload pointing elsewhere must not move a sibling transcript."""

    db_path, context, _payload = _restart_scenario(tmp_path)
    _seed_second_child(db_path)
    _claim_child_gated_tool(db_path, context, request_id=f"{SECOND_CHILD_PROCESS_ID}:1")
    _update(
        db_path,
        "UPDATE jobs SET status='blocked' WHERE job_id=?",
        (SECOND_CHILD_JOB_ID,),
    )
    # The scanned child's payload now speaks for its already blocked sibling.
    _update(
        db_path,
        "UPDATE job_payloads SET payload_json="
        "(SELECT payload_json FROM job_payloads WHERE job_id=?) WHERE job_id=?",
        (SECOND_CHILD_JOB_ID, CHILD_JOB_ID),
    )

    outcome = _recover_children(db_path)

    assert (outcome.blocked_child_count, outcome.collected_invocation_count) == (2, 0)
    for process_id in (CHILD_PROCESS_ID, SECOND_CHILD_PROCESS_ID):
        assert _transcript(db_path, process_id) == ()


def test_payload_naming_another_parent_is_quarantined_not_settled(
    tmp_path: Path,
) -> None:
    """A mismatched parent must not fail the settle transaction on every start."""

    db_path, _context, _payload = _restart_scenario(tmp_path)
    _update(
        db_path,
        "UPDATE job_payloads SET payload_json="
        "json_set(payload_json,'$.parent_process_id','process-other')"
        " WHERE job_id=?",
        (CHILD_JOB_ID,),
    )
    _fence_parent(db_path)

    outcome = _recover_children(db_path)

    assert (outcome.blocked_child_count, outcome.settled_child_count) == (1, 0)
    assert _job_state(db_path, CHILD_JOB_ID) == (
        "blocked",
        "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR",
    )


def test_already_blocked_paused_child_still_releases_its_approval(
    tmp_path: Path,
) -> None:
    """A prior block must not leave the blocker showing on the root forever."""

    db_path, _context, payload = _restart_scenario(tmp_path)
    session_id, _request_id = snapshot._pause_child(db_path, payload, name="b")
    _update(db_path, "UPDATE jobs SET status='blocked' WHERE job_id=?", (CHILD_JOB_ID,))

    assert _recover_children(db_path).blocked_child_count == 1

    with sqlite3.connect(db_path) as connection:
        status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id=?",
            (session_id,),
        ).fetchone()
    assert status == ("interrupted",)
    assert _root_blocker_counts(db_path) == [1, 0]
    # The release is idempotent, so a second start adds no further snapshot.
    assert _recover_children(db_path).blocked_child_count == 1
    assert _root_blocker_counts(db_path) == [1, 0]


def test_recovered_attachment_read_is_reported_as_unsupported(
    tmp_path: Path,
) -> None:
    """An attachment output cannot reach a child, so it must not read as success."""

    db_path, context, _payload = _restart_scenario(tmp_path)
    _claim_child_gated_tool(db_path, context)
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        completion=ToolInvocationCompletionInput(
            invocation_id=INVOCATION_ID,
            status="completed",
            completed_at=TIMESTAMP,
            output_json={
                "kind": "attachment",
                "path": "diagram.png",
                "mime_type": "image/png",
                "message": "Image read successfully",
                "attachments": [{"type": "file", "path": "diagram.png"}],
            },
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    assert _recover_children(db_path).collected_invocation_count == 1

    entry = _transcript(db_path, CHILD_PROCESS_ID)[-1]
    assert "SUBAGENT_ATTACHMENT_READ_UNSUPPORTED" in entry
    assert "Image read successfully" not in entry

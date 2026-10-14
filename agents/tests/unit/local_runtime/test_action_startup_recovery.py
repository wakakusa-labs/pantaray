from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
)
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    EXECUTION_CONTEXT_STATE_FIELDS,
)
from pantaray_agents.local_runtime.runtime.action_startup_recovery import (
    recover_interrupted_action_runs_for_startup,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import (
    repair_inflight_jobs_for_startup,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ToolInvocationCompletionInput,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_apply_patch_invocation,
)

TIMESTAMP = "2026-03-23T00:00:00Z"


def _seed_interrupted_run(tmp_path: Path) -> tuple[Path, ActionExecutionContext]:
    db_path, raw_context = bootstrap_runtime_db(tmp_path)
    context = cast(ActionExecutionContext, raw_context)
    payload = json.dumps(
        {
            "job_id": "job-1",
            "process_id": "process-1",
            "action_id": "action-1",
            "user_id": "user-1",
            "continuation_ref": {"kind": "user_step", "user_step_id": "user-1"},
        }
    )
    with sqlite3.connect(db_path) as connection, connection:
        connection.executescript(
            f"""INSERT INTO processes(process_id,user_id,kind,status,suggestion_id,action_id,current_job_id,started_at,updated_at,heartbeat_at,next_event_seq)
                VALUES ('process-1','user-1','action','running','suggestion-1','action-1','job-1','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}',1);
                INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,claimed_by,claimed_at,heartbeat_at,scheduled_at,started_at,logical_key)
                VALUES ('job-1','user-1','execute_action','process-1','running',1,'worker-1','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}','action-1');
                INSERT INTO job_attempts(attempt_id,job_id,attempt_number,started_at,status)
                VALUES ('attempt-1','job-1',1,'{TIMESTAMP}','running');
                INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,local_step_number,short_step_id,step_type,step_name,status,goal_handle,user_request_text,user_message_id,user_message_json,accepted_sequence,adopted_process_id,started_at,completed_at,created_at)
                VALUES ('user-1','action-1','user-1',1,1,'S-1-USER','user_request','user_request','success','S','start','message-1','{{}}',1,'process-1','{TIMESTAMP}','{TIMESTAMP}','{TIMESTAMP}');"""
        )
        connection.execute(
            "INSERT INTO job_payloads(job_id,payload_json) VALUES ('job-1',?)",
            (payload,),
        )
    return db_path, context


def _insert_checkpoint(
    db_path: Path, context: ActionExecutionContext, *, pending: bool = False
) -> None:
    state: dict[str, object] = {
        "manifest_id": context.manifest_id,
        "execution_session_id": context.execution_session_id,
        "execution_network_policy": context.network_policy,
        "action_temp_dir": str(context.action_temp_dir),
        "app_runtime_python": str(context.app_runtime_python),
        "read_access_scope": context.read_access_scope,
        "preserved": "yes",
    }
    if pending:
        state["pending_approval_request"] = {
            "approval_session_id": "approval-1",
            "tool_request_id": "request-1",
        }
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,local_step_number,short_step_id,step_type,step_name,status,runtime_state_checkpoint,runtime_state_checkpoint_version,created_at)
               VALUES ('checkpoint','action-1','user-1',2,2,'S-2-THINK','llm_output','thinking','success',?,?,?)""",
            (json.dumps(state), RUNTIME_STATE_CHECKPOINT_VERSION, TIMESTAMP),
        )


def test_recovery_cleans_exact_run_and_retry_gets_new_session(tmp_path: Path) -> None:
    db_path, context = _seed_interrupted_run(tmp_path)
    _insert_checkpoint(db_path, context)
    register_running_apply_patch_invocation(db_path=db_path, context=context)

    assert (
        recover_interrupted_action_runs_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )
        == 1
    )
    with sqlite3.connect(db_path) as connection:
        checkpoint = json.loads(
            connection.execute(
                "SELECT runtime_state_checkpoint FROM agent_action_steps WHERE step_id='checkpoint'"
            ).fetchone()[0]
        )
        states = connection.execute(
            """SELECT (SELECT status FROM jobs WHERE job_id='job-1'), (SELECT status FROM processes WHERE process_id='process-1'),
                      (SELECT status FROM job_attempts WHERE attempt_id='attempt-1'), (SELECT status FROM execution_sessions WHERE execution_session_id=?),
                      (SELECT status FROM tool_runtime_resources WHERE action_id='action-1' AND tool_invocation_id IS NULL)""",
            (context.execution_session_id,),
        ).fetchone()
    assert states == ("queued", "enqueued", "failed", "expired", "cleaned")
    assert checkpoint["preserved"] == "yes"
    assert not any(field in checkpoint for field in EXECUTION_CONTEXT_STATE_FIELDS)
    assert not context.action_temp_dir.exists()
    retry = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:01Z",
        allowed_tool_ids=("read",),
    )
    assert retry.execution_session_id != context.execution_session_id


@pytest.mark.parametrize("session_status", ["failed", "expired"])
def test_recovery_resumes_when_exact_leaf_is_already_missing(
    tmp_path: Path, session_status: str
) -> None:
    db_path, context = _seed_interrupted_run(tmp_path)
    context.action_temp_dir.rmdir()
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE execution_sessions SET status=?,completed_at=?",
            (session_status, TIMESTAMP),
        )
    assert (
        recover_interrupted_action_runs_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )
        == 1
    )


def test_pending_approval_requeues_job_without_cleaning_session(tmp_path: Path) -> None:
    db_path, context = _seed_interrupted_run(tmp_path)
    _insert_checkpoint(db_path, context, pending=True)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """INSERT INTO approval_sessions(approval_session_id,user_id,action_id,manifest_id,tool_request_id,tool_id,intent_class,approval_source,status,approved_capabilities_json,command_summary_json,requested_at,created_at)
               VALUES ('approval-1','user-1','action-1',?,'request-1','read','read_only','prompt','pending','[]','{}',?,?)""",
            (context.manifest_id, TIMESTAMP, TIMESTAMP),
        )
    assert (
        recover_interrupted_action_runs_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )
        == 0
    )
    assert repair_inflight_jobs_for_startup(db_path, 1_000) == 1
    with sqlite3.connect(db_path) as connection:
        states = connection.execute(
            """SELECT (SELECT status FROM jobs WHERE job_id='job-1'), (SELECT status FROM execution_sessions WHERE execution_session_id=?),
                      (SELECT status FROM tool_runtime_resources WHERE action_id='action-1' AND tool_invocation_id IS NULL)""",
            (context.execution_session_id,),
        ).fetchone()
    assert states == ("queued", "running", "active")
    assert context.action_temp_dir.exists()


def test_resumed_approval_run_past_its_approval_is_requeued(tmp_path: Path) -> None:
    db_path, context = _seed_interrupted_run(tmp_path)
    # The resumed run claimed the approval and moved on: its newest checkpoint
    # no longer names the approval its job payload still continues from.
    _insert_checkpoint(db_path, context)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """INSERT INTO approval_sessions(approval_session_id,user_id,action_id,manifest_id,tool_request_id,tool_invocation_id,tool_id,intent_class,approval_source,status,approved_capabilities_json,command_summary_json,requested_at,decided_at,claimed_at,created_at)
               VALUES ('approval-1','user-1','action-1',?,'request-1','invocation-1','read','read_only','prompt','approved_once','[]','{}',?,?,?,?)""",
            (context.manifest_id, TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            "UPDATE job_payloads SET payload_json=json_set(payload_json,"
            "'$.continuation_ref',json(?)) WHERE job_id='job-1'",
            (
                json.dumps(
                    {
                        "kind": "tool_approval",
                        "approval_session_id": "approval-1",
                        "tool_request_id": "request-1",
                    }
                ),
            ),
        )

    assert (
        recover_interrupted_action_runs_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )
        == 1
    )
    with sqlite3.connect(db_path) as connection:
        states = connection.execute(
            """SELECT (SELECT status FROM jobs WHERE job_id='job-1'), (SELECT status FROM execution_sessions WHERE execution_session_id=?),
                      (SELECT status FROM tool_runtime_resources WHERE action_id='action-1' AND tool_invocation_id IS NULL)""",
            (context.execution_session_id,),
        ).fetchone()
    assert states == ("queued", "expired", "cleaned")

    # The re-claimed run opens a new session and stops again before it saves a
    # checkpoint, so the newest one still has no execution context.
    assert claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-2",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=now_utc_iso(),
        allowed_tool_ids=("read",),
    )
    assert (
        recover_interrupted_action_runs_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )
        == 1
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT status FROM jobs WHERE job_id='job-1'"
        ).fetchone() == ("queued",)


def _seed_parallel_batch_with_one_row_lost(
    db_path: Path, context: ActionExecutionContext
) -> str:
    """Two commands of one turn: both ran, but only the later one's row landed."""

    think = {
        "step_id": "think-2",
        "step_number": 2,
        "phase": "executing",
        "step_type": "llm_output",
        "summary": "",
        "tool_id": None,
        "short_step_id": "S-2-THINK",
        "started_at": TIMESTAMP,
        "completed_at": TIMESTAMP,
    }
    later = {
        "step_id": "tool-b",
        "step_number": 3,
        "phase": "executing",
        "step_type": "tool_execution",
        "summary": "count the files",
        "tool_id": "bash",
        "output": {"stdout": "b"},
        "call_id": "call-b",
        "llm_step_id": "think-2",
        "started_at": TIMESTAMP,
        "completed_at": TIMESTAMP,
    }
    state = {
        "manifest_id": context.manifest_id,
        "execution_session_id": context.execution_session_id,
        "execution_network_policy": context.network_policy,
        "action_temp_dir": str(context.action_temp_dir),
        "app_runtime_python": str(context.app_runtime_python),
        "read_access_scope": context.read_access_scope,
        "history_by_scope": {"S": [think, later]},
        "next_action": None,
        "step": 3,
        "llm_steps_taken": 1,
        "tool_steps_taken": 1,
        "steps_taken": 2,
    }
    turn = {
        "mode": "action_turn",
        "messages": [],
        "calls": [
            {
                "call_id": call,
                "name": "bash",
                "arguments": {"command": command, "step_note": note},
            }
            for call, command, note in (
                ("call-a", "touch made-once", "make the file"),
                ("call-b", "ls | wc -l", "count the files"),
            )
        ],
    }
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,
                 local_step_number,short_step_id,step_type,step_name,status,
                 llm_response_text,created_at)
               VALUES ('think-2','action-1','user-1',2,2,'S-2-THINK','llm_output',
                 'supervisor_think','success',?,?)""",
            (json.dumps(turn), TIMESTAMP),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,
                 local_step_number,short_step_id,step_type,step_name,status,
                 runtime_state_checkpoint,runtime_state_checkpoint_version,
                 call_id,llm_step_id,completed_at,created_at)
               VALUES ('tool-b','action-1','user-1',3,3,'S-3-TOOL','tool_execution',
                 'tool::bash','success',?,?,'call-b','think-2',?,?)""",
            (json.dumps(state), RUNTIME_STATE_CHECKPOINT_VERSION, TIMESTAMP, TIMESTAMP),
        )
    # The earlier command finished and stored its output, then the run died.
    invocation_id = register_running_apply_patch_invocation(
        db_path=db_path, context=context
    )
    step_id = ActionToolCallOrigin(
        llm_step_id="think-2", call_id="call-a"
    ).shared_turn_step_id()
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE tool_invocations SET tool_request_id=? WHERE invocation_id=?",
            (f"action-1:{step_id}:1", invocation_id),
        )
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id=invocation_id,
            status="completed",
            completed_at=TIMESTAMP,
            output_json={"status": "success", "exit_code": 0, "stdout": "a"},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )
    return step_id


def test_restart_gives_back_a_parallel_command_that_ran_before_its_row_landed(
    tmp_path: Path,
) -> None:
    """Without its result the resumed run would plan the command as a new call
    and run it a second time."""

    db_path, context = _seed_interrupted_run(tmp_path)
    step_id = _seed_parallel_batch_with_one_row_lost(db_path, context)

    assert (
        recover_interrupted_action_runs_for_startup(
            db_path=db_path, busy_timeout_ms=1_000
        )
        == 1
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT step_id, runtime_state_checkpoint FROM agent_action_steps
               WHERE runtime_state_checkpoint IS NOT NULL"""
        ).fetchall()
        restored_row = connection.execute(
            "SELECT step_number, call_id, status FROM agent_action_steps WHERE step_id=?",
            (step_id,),
        ).fetchone()
    assert [row[0] for row in rows] == ["tool-b"]
    resumed = json.loads(rows[0][1])
    entries = resumed["history_by_scope"]["S"]
    # Both calls answered, in the order the THINK declared them.
    assert [entry.get("call_id") for entry in entries] == [None, "call-a", "call-b"]
    assert entries[1]["output"] == {"status": "success", "exit_code": 0, "stdout": "a"}
    assert entries[1]["summary"] == "make the file"
    assert (resumed["next_action"], resumed["step"]) == (None, 4)
    assert restored_row == (2, "call-a", "success")

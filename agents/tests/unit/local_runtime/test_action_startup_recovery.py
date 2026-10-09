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
from pantaray_agents.local_runtime.storage.migrations import (
    repair_inflight_jobs_for_startup,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext

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

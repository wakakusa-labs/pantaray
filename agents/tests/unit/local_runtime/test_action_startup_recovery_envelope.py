from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.action_startup_recovery_envelope import (
    load_action_startup_recovery_envelope_in_connection,
)
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.resources.resource_db_support import (
    configure_connection,
)

from .local_action_repository_support import (
    ACTION_ID,
    BUSY_TIMEOUT_MS,
    SUGGESTION_ID,
    USER_ID,
    bootstrap_action_repository_db,
)

JOB_ID = "job-1"
PROCESS_ID = "process-1"
ROOT_STEP_ID = "user-1"
TIMESTAMP = "2026-08-29T00:00:00Z"


def _payload(*, job_id: str, process_id: str, continuation: object) -> str:
    return json.dumps(
        {
            "job_id": job_id,
            "process_id": process_id,
            "action_id": ACTION_ID,
            "user_id": USER_ID,
            "continuation_ref": continuation,
        }
    )


def _seed_user_run(tmp_path: Path, *, action_status: str = "processing") -> Path:
    db_path = bootstrap_action_repository_db(tmp_path)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE agent_actions SET status=? WHERE action_id=?",
            (action_status, ACTION_ID),
        )
        connection.execute(
            """INSERT INTO processes(
                process_id,user_id,kind,status,suggestion_id,action_id,current_job_id,
                started_at,updated_at,heartbeat_at,next_event_seq
            ) VALUES (?,?,'action','running',?,?,?, ?,?,?,1)""",
            (
                PROCESS_ID,
                USER_ID,
                SUGGESTION_ID,
                ACTION_ID,
                JOB_ID,
                TIMESTAMP,
                TIMESTAMP,
                TIMESTAMP,
            ),
        )
        connection.execute(
            """INSERT INTO jobs(
                job_id,user_id,job_type,process_id,status,attempt,claimed_by,
                claimed_at,heartbeat_at,scheduled_at,started_at,logical_key
            ) VALUES (?,?,'execute_action',?,'running',1,'worker-1',?,?,?,?,?)""",
            (
                JOB_ID,
                USER_ID,
                PROCESS_ID,
                TIMESTAMP,
                TIMESTAMP,
                TIMESTAMP,
                TIMESTAMP,
                ACTION_ID,
            ),
        )
        connection.execute(
            "INSERT INTO job_payloads(job_id,payload_json) VALUES (?,?)",
            (
                JOB_ID,
                _payload(
                    job_id=JOB_ID,
                    process_id=PROCESS_ID,
                    continuation={"kind": "user_step", "user_step_id": ROOT_STEP_ID},
                ),
            ),
        )
        connection.execute(
            """INSERT INTO job_attempts(
                attempt_id,job_id,attempt_number,started_at,status
            ) VALUES ('attempt-1',?,1,?,'running')""",
            (JOB_ID, TIMESTAMP),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,goal_handle,user_request_text,user_message_id,
                user_message_json,accepted_sequence,adopted_process_id,
                started_at,completed_at,created_at
            ) VALUES (?,?,?,1,1,'S-1-USER','user_request','user_request','success',
                      'S','start','message-1','{}',1,?,?,?,?)""",
            (
                ROOT_STEP_ID,
                ACTION_ID,
                USER_ID,
                PROCESS_ID,
                TIMESTAMP,
                TIMESTAMP,
                TIMESTAMP,
            ),
        )
    return db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    configure_connection(connection, BUSY_TIMEOUT_MS)
    connection.execute("BEGIN")
    return connection


@pytest.mark.parametrize("action_status", ["queued", "processing"])
def test_user_run_binds_exact_envelope_and_anchor_without_writes(
    tmp_path: Path,
    action_status: str,
) -> None:
    db_path = _seed_user_run(tmp_path, action_status=action_status)
    with _connect(db_path) as connection:
        envelope = load_action_startup_recovery_envelope_in_connection(
            connection=connection, job_id=JOB_ID
        )
        assert connection.total_changes == 0

    assert envelope.job_id == JOB_ID
    assert envelope.action_status == action_status
    assert envelope.lineage.root_process_id == PROCESS_ID
    assert envelope.anchor.step_id == ROOT_STEP_ID
    assert envelope.anchor.step_number == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE job_attempts SET status='completed',completed_at='now'",
        "UPDATE job_payloads SET payload_json=json_set(payload_json,'$.process_id','other')",
        "UPDATE agent_actions SET status='success'",
    ],
)
def test_envelope_rejects_current_attempt_or_strict_payload_mismatch(
    tmp_path: Path, mutation: str
) -> None:
    db_path = _seed_user_run(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(mutation)
    with (
        _connect(db_path) as connection,
        pytest.raises(MigrationError, match="envelope"),
    ):
        load_action_startup_recovery_envelope_in_connection(
            connection=connection, job_id=JOB_ID
        )


def test_approval_run_uses_exact_latest_matching_anchor(tmp_path: Path) -> None:
    db_path = _seed_user_run(tmp_path)
    approval = {"approval_session_id": "approval-1", "tool_request_id": "request-1"}
    pending = {"pending_approval_request": approval}
    blockers = {"current_approval_blockers": [approval]}
    with sqlite3.connect(db_path) as connection, connection:
        # An approved tool resumes the paused process itself and rewrites its payload.
        connection.execute(
            "UPDATE job_payloads SET payload_json=? WHERE job_id=?",
            (
                _payload(
                    job_id=JOB_ID,
                    process_id=PROCESS_ID,
                    continuation={"kind": "tool_approval", **approval},
                ),
                JOB_ID,
            ),
        )
        connection.execute(
            "UPDATE agent_action_steps SET runtime_state_checkpoint=?, "
            "runtime_state_checkpoint_version=1 WHERE step_id=?",
            (json.dumps(pending), ROOT_STEP_ID),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,runtime_state_checkpoint,
                runtime_state_checkpoint_version,created_at
            ) VALUES ('anchor-2',?,?,2,2,'S-2-THINK','llm_output','thinking',
                      'success',?,1,'later')""",
            (ACTION_ID, USER_ID, json.dumps(blockers)),
        )

    with _connect(db_path) as connection:
        envelope = load_action_startup_recovery_envelope_in_connection(
            connection=connection, job_id=JOB_ID
        )
    assert envelope.lineage.root_process_id == PROCESS_ID
    assert envelope.anchor.step_id == "anchor-2"

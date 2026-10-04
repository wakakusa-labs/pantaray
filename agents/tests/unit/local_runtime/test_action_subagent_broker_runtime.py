from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.runtime.action_subagent_broker_authority import (
    ActionSubagentBrokerAuthorityError,
    load_action_subagent_broker_authority,
)
from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.job_claim import claim_next_pending_job
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.tooling import ActionExecutionContext
from pantaray_agents.tasks.types import ActionSubagentJobPayload
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS

from .resource_recovery_test_support import bootstrap_runtime_db

TIMESTAMP = "2026-09-01T00:00:00Z"


def _running_child(
    tmp_path: Path,
) -> tuple[Path, ActionExecutionContext, ActionSubagentJobPayload]:
    db_path, context = bootstrap_runtime_db(tmp_path)
    payload = build_action_subagent_job_payload(
        {
            "job_id": "child-job",
            "process_id": "child-process",
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": "parent-process",
            "inference_profile_id": SUBAGENT_MODEL_SETTINGS[0].profile_id,
            "action_context": "# Workspace Paths\nparent context",
            "task": "Inspect the assigned boundary",
            "context_refs": [],
            "resource_claim_ids": ["claim-1"],
        }
    )
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "INSERT INTO processes(process_id,user_id,kind,status,action_id,"
            "current_job_id,started_at,updated_at,heartbeat_at,next_event_seq) VALUES "
            "('parent-process','user-1','action','running','action-1',"
            "'parent-job',?,?,?,1)",
            (TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            "INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,"
            "scheduled_at,started_at,logical_key) VALUES "
            "('parent-job','user-1','execute_action','parent-process','running',"
            "1,?,?,'action-1')",
            (TIMESTAMP, TIMESTAMP),
        )
        enqueue_action_subagent_job_in_connection(
            connection=connection, payload=payload, scheduled_at=TIMESTAMP
        )
        connection.execute(
            "INSERT INTO action_subagent_resource_claims("
            "claim_id,user_id,action_id,parent_process_id,child_process_id,"
            "resource_kind,root_identity,normalized_key,acquired_at) VALUES "
            "('claim-1','user-1','action-1','parent-process','child-process',"
            "'workspace_path',?,'.',?)",
            (context.manifest_id, TIMESTAMP),
        )
    assert claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type="execute_action_subagent",
        owner_user_id="user-1",
        claimed_by="test-worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    return db_path, context, payload


def test_exact_running_child_derives_root_broker_authority(tmp_path: Path) -> None:
    db_path, context, payload = _running_child(tmp_path)

    authority = load_action_subagent_broker_authority(
        db_path=db_path, busy_timeout_ms=1_000, payload=payload
    )

    assert authority.manifest_id == context.manifest_id
    assert authority.execution_session_id == context.execution_session_id


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE action_subagent_resource_claims SET released_at='2026-09-01T00:01:00Z'",
        "UPDATE processes SET status='failed' WHERE process_id='child-process'",
    ),
    ids=("released-claim", "nonrunning-child"),
)
def test_released_or_nonrunning_child_cannot_reuse_broker_authority(
    tmp_path: Path, mutation: str
) -> None:
    db_path, _context, payload = _running_child(tmp_path)
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(mutation)

    with pytest.raises(ActionSubagentBrokerAuthorityError):
        load_action_subagent_broker_authority(
            db_path=db_path,
            busy_timeout_ms=1_000,
            payload=payload,
        )


def test_cross_child_payload_cannot_reuse_broker_authority(tmp_path: Path) -> None:
    db_path, _context, payload = _running_child(tmp_path)
    payload["process_id"] = "other-child"

    with pytest.raises(ActionSubagentBrokerAuthorityError):
        load_action_subagent_broker_authority(
            db_path=db_path,
            busy_timeout_ms=1_000,
            payload=payload,
        )

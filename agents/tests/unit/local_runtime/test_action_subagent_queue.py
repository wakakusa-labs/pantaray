from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    build_action_subagent_enqueue_request,
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.job_enqueue import enqueue_local_job
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_action_subagent_job_payload_json,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.tasks.types import ActionSubagentJobPayload

from .migrated_db import prepare_test_database

TIMESTAMP = "2026-09-01T00:00:00Z"


def _payload(*, task: str = "Inspect the queue boundary") -> ActionSubagentJobPayload:
    return build_action_subagent_job_payload(
        {
            "job_id": "child-job-1",
            "process_id": "child-process-1",
            "user_id": "user-1",
            "action_id": "action-1",
            "parent_process_id": "parent-process-1",
            "inference_profile_id": "action.subagent.luna.high.v1",
            "action_context": "# Workspace Paths\nparent context",
            "task": task,
            "context_refs": ["conversation:step-1"],
            "resource_claim_ids": ["claim-1"],
        }
    )


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, 1_000)
        with connection:
            connection.execute(
                "INSERT INTO users(user_id,ui_language,created_at,updated_at) "
                "VALUES ('user-1','ja',?,?)",
                (TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO agent_actions(action_id,user_id,initial_user_message_id,"
                "execution_target_json,status,final_output,prompt_name,prompt_version,"
                "created_at,updated_at) VALUES "
                "('action-1','user-1','message-1','{\"kind\":\"scratch\"}',"
                "'processing','','action','1',?,?)",
                (TIMESTAMP, TIMESTAMP),
            )
            connection.execute(
                "INSERT INTO processes(process_id,user_id,kind,status,action_id,"
                "started_at,updated_at,heartbeat_at,next_event_seq) VALUES "
                "('parent-process-1','user-1','action','running','action-1',?,?,?,1)",
                (TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
    return db_path


def test_action_subagent_payload_closes_shape_and_unicode_bounds() -> None:
    emoji_task = "🧪" * 32_000
    payload = _payload(task=emoji_task)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert parse_action_subagent_job_payload_json(encoded)["task"] == emoji_task
    assert len(emoji_task.encode("utf-8")) > len(emoji_task)

    raw = dict(payload)
    raw["provider"] = "openai"
    with pytest.raises(MigrationError, match="unexpected fields: provider"):
        parse_action_subagent_job_payload_json(json.dumps(raw, ensure_ascii=False))

    raw = dict(payload)
    del raw["parent_process_id"]
    with pytest.raises(MigrationError, match="parent_process_id"):
        parse_action_subagent_job_payload_json(json.dumps(raw, ensure_ascii=False))

    with pytest.raises(MigrationError, match="32,000 Unicode code points"):
        _payload(task="🧪" * 32_001)
    oversized = dict(_payload())
    oversized["context_refs"] = ["界" * 262_144]
    with pytest.raises(MigrationError, match="262144 UTF-8 bytes"):
        parse_action_subagent_job_payload_json(
            json.dumps(oversized, ensure_ascii=False)
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("context_refs", ["ref"] * 65, "context_refs exceeds 64 items"),
        ("resource_claim_ids", ["claim", ""], r"resource_claim_ids\[1\]"),
    ),
)
def test_action_subagent_payload_rejects_invalid_opaque_refs(
    field: str,
    value: list[str],
    error: str,
) -> None:
    raw = dict(_payload())
    raw[field] = value
    with pytest.raises(MigrationError, match=error):
        parse_action_subagent_job_payload_json(json.dumps(raw))


def test_enqueue_persists_lineage_and_rejects_identity_mismatch(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    payload = _payload()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, 1_000)
        with connection:
            malformed_raw = dict(payload)
            malformed_raw["provider"] = "openai"
            malformed = cast(ActionSubagentJobPayload, malformed_raw)
            with pytest.raises(MigrationError, match="unexpected fields: provider"):
                enqueue_action_subagent_job_in_connection(
                    connection=connection, payload=malformed, scheduled_at=TIMESTAMP
                )
            inserted = enqueue_action_subagent_job_in_connection(
                connection=connection, payload=payload, scheduled_at=TIMESTAMP
            )
            replay = enqueue_action_subagent_job_in_connection(
                connection=connection, payload=payload, scheduled_at=TIMESTAMP
            )

            changed_profile = cast(ActionSubagentJobPayload, dict(payload))
            changed_profile["inference_profile_id"] = "action.subagent.sol.high.v1"
            with pytest.raises(MigrationError, match="different identity"):
                enqueue_action_subagent_job_in_connection(
                    connection=connection,
                    payload=changed_profile,
                    scheduled_at=TIMESTAMP,
                )

            request = build_action_subagent_enqueue_request(
                payload, scheduled_at=TIMESTAMP
            )
            request["process_parent_process_id"] = "different-parent"
            with pytest.raises(MigrationError, match="different identity"):
                enqueue_local_job(connection=connection, request=request)

        process = connection.execute(
            "SELECT kind,status,action_id,parent_process_id FROM processes "
            "WHERE process_id='child-process-1'"
        ).fetchone()
        stored = connection.execute(
            "SELECT j.job_type,j.process_id,p.payload_json FROM jobs AS j JOIN "
            "job_payloads AS p USING(job_id) WHERE j.job_id='child-job-1'"
        ).fetchone()

    assert inserted["inserted_new"] is True
    assert replay == {**inserted, "inserted_new": False}
    assert process == ("action_subagent", "enqueued", "action-1", "parent-process-1")
    assert stored is not None
    assert stored[:2] == ("execute_action_subagent", "child-process-1")
    assert parse_action_subagent_job_payload_json(stored[2]) == payload


def test_caller_rollback_removes_child_process_job_and_payload(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, 1_000)
        connection.execute("BEGIN IMMEDIATE")
        enqueue_action_subagent_job_in_connection(
            connection=connection, payload=_payload(), scheduled_at=TIMESTAMP
        )
        connection.rollback()
        counts = connection.execute(
            "SELECT (SELECT COUNT(*) FROM jobs),(SELECT COUNT(*) FROM job_payloads)"
        ).fetchone()
        child_count = connection.execute(
            "SELECT COUNT(*) FROM processes WHERE kind='action_subagent'"
        ).fetchone()[0]

    assert counts == (0, 0)
    assert child_count == 0

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.application.action.resume_service import (
    ActionResumeService,
    ResumeDeps,
)
from pantaray_agents.local_runtime.runtime import (
    action_job_runtime_repository,
    job_payload_models,
)
from pantaray_agents.local_runtime.runtime.action_approval import (
    ActionApprovalDecisionCommand,
    ActionApprovalDecisionError,
    apply_action_approval_decision,
)
from pantaray_agents.local_runtime.runtime.action_cancel_repository import (
    ActionCancelRepository,
)
from pantaray_agents.local_runtime.runtime.action_message_process_fence import (
    resolve_action_process_lineage_in_connection,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ExistingActionTarget,
    NewActionTarget,
    SubmitActionMessageCommand,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.action_user_adoption import (
    adopt_pending_action_user_steps_at_parent_think,
)
from pantaray_agents.local_runtime.runtime.job_claim import (
    claim_next_pending_job,
)
from pantaray_agents.local_runtime.runtime.process_events import (
    mark_local_action_job_paused,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.command_approval_summaries import (
    build_apply_patch_summary,
)
from pantaray_agents.schema.agent.action import (
    ActionAgentRequest,
    ActionUserMessageInput,
)
from pantaray_agents.schema.agent.base import AgentError
from pantaray_agents.tasks.action_job import _run_action_job
from pantaray_agents.tasks.action_user_message import serialize_action_user_message

from .local_action_repository_support import build_action_repository
from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000


@dataclass(frozen=True, slots=True)
class PendingApprovalFixture:
    db_path: Path
    action_id: str
    predecessor_job_id: str
    predecessor_process_id: str
    user_step_id: str


@pytest.fixture(autouse=True)
def _reset_session_store() -> None:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()


@pytest.fixture
def pending_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> PendingApprovalFixture:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, BUSY_TIMEOUT_MS, load_default_migrations())
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version="1",
    )
    created = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id="message-1",
                content="Continue after tool approval",
            ),
        )
    )
    checkpoint = json.dumps(
        {
            "pending_approval_request": {
                "approval_session_id": "approval-1",
                "tool_request_id": "tool-request-1",
            },
            "current_approval_blockers": [],
        }
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                "UPDATE agent_actions SET status = 'processing' WHERE action_id = ?",
                (created.action_id,),
            )
            connection.execute(
                """
                UPDATE agent_action_steps
                SET runtime_state_checkpoint = ?,
                    runtime_state_checkpoint_version = 1
                WHERE step_id = ?
                """,
                (checkpoint, created.user_step_id),
            )
            connection.execute(
                """
                INSERT INTO approval_sessions(
                    approval_session_id,
                    user_id,
                    action_id,
                    tool_id,
                    intent_class,
                    approval_source,
                    status,
                    approved_capabilities_json,
                    command_summary_json,
                    requested_at,
                    created_at,
                    tool_request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "approval-1",
                    "user-1",
                    created.action_id,
                    "tool-1",
                    "read_only",
                    "prompt",
                    "pending",
                    '{"required_capabilities":[]}',
                    "{}",
                    "2026-08-16T00:00:20Z",
                    "2026-08-16T00:00:20Z",
                    "tool-request-1",
                ),
            )

    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    assert claimed["job_id"] == created.job_id
    mark_local_action_job_paused(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_id=created.job_id,
        process_id=created.process_id,
        payload={
            "action_id": created.action_id,
            "user_id": "user-1",
            "status": "processing",
            "completed_at": "2026-08-16T00:00:20Z",
            "reason": "approval_pending",
            "approval_blockers": [
                {
                    "approval_session_id": "approval-1",
                    "tool_request_id": "tool-request-1",
                }
            ],
        },
    )
    with sqlite3.connect(db_path) as connection:
        paused_state = connection.execute(
            """
            SELECT jobs.status, jobs.claimed_by, jobs.claimed_at,
                   processes.status, processes.current_job_id,
                   job_attempts.status
            FROM jobs
            JOIN processes ON processes.process_id = jobs.process_id
            JOIN job_attempts ON job_attempts.job_id = jobs.job_id
            WHERE jobs.job_id = ?
            """,
            (created.job_id,),
        ).fetchone()
    assert paused_state == ("paused", None, None, "paused", None, "completed")
    return PendingApprovalFixture(
        db_path=db_path,
        action_id=created.action_id,
        predecessor_job_id=created.job_id,
        predecessor_process_id=created.process_id,
        user_step_id=created.user_step_id,
    )


@pytest.mark.parametrize("decision", ["approved_once", "denied"])
@pytest.mark.asyncio
async def test_action_approval_decision_resumes_the_same_runtime(
    pending_approval: PendingApprovalFixture,
    decision: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = pending_approval.db_path
    action_id = pending_approval.action_id
    steer = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=ExistingActionTarget(
                action_id=action_id,
                expected_process_id=pending_approval.predecessor_process_id,
            ),
            message=ActionUserMessageInput(
                message_id="message-steer",
                content="Continue after this approval",
            ),
        )
    )
    command = ActionApprovalDecisionCommand(
        user_id="user-1",
        action_id=action_id,
        process_id=pending_approval.predecessor_process_id,
        tool_request_id="tool-request-1",
        approval_session_id="approval-1",
        decision=decision,
    )
    result = apply_action_approval_decision(command)
    opposite_decision = "denied" if decision == "approved_once" else "approved_once"
    for replay_decision, process_id, error_code in (
        (decision, command.process_id, "APPROVAL_DECISION_ALREADY_APPLIED"),
        (decision, "different-process", "APPROVAL_DECISION_CONFLICT"),
        (opposite_decision, command.process_id, "APPROVAL_DECISION_CONFLICT"),
    ):
        with pytest.raises(ActionApprovalDecisionError) as replay_error:
            apply_action_approval_decision(
                command.model_copy(
                    update={"decision": replay_decision, "process_id": process_id}
                )
            )
        assert replay_error.value.error_code == error_code

    with sqlite3.connect(db_path) as connection:
        approval_status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id = 'approval-1'"
        ).fetchone()
        payload_row = connection.execute(
            "SELECT payload_json FROM job_payloads WHERE job_id = ?",
            (result.job_id,),
        ).fetchone()
        steer_row = connection.execute(
            """
            SELECT step_number, adopted_process_id, adoption_canceled_at
            FROM agent_action_steps WHERE step_id = ?
            """,
            (steer.user_step_id,),
        ).fetchone()
        public_event = connection.execute(
            """
            SELECT suggestion_id, event_name, payload
            FROM agent_process_events
            WHERE action_id = ? AND event_name = 'action_resume_requested'
            """,
            (action_id,),
        ).fetchone()
        predecessor_job = connection.execute(
            """
            SELECT status, completed_at, claimed_by, claimed_at
            FROM jobs
            WHERE job_id = ?
            """,
            (pending_approval.predecessor_job_id,),
        ).fetchone()
        predecessor_process = connection.execute(
            """
            SELECT status, completed_at, current_job_id
            FROM processes
            WHERE process_id = ?
            """,
            (pending_approval.predecessor_process_id,),
        ).fetchone()
        runtime_count = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM jobs WHERE logical_key = ?),
              (SELECT COUNT(*) FROM processes WHERE action_id = ?)
            """,
            (action_id, action_id),
        ).fetchone()
        running_attempt_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM job_attempts
            WHERE job_id = ? AND status = 'running'
            """,
            (pending_approval.predecessor_job_id,),
        ).fetchone()[0]

    assert result.decision == decision
    assert approval_status == (decision,)
    assert payload_row is not None
    assert json.loads(payload_row[0])["continuation_ref"] == {
        "kind": "tool_approval",
        "approval_session_id": "approval-1",
        "tool_request_id": "tool-request-1",
    }
    assert result.job_id == pending_approval.predecessor_job_id
    assert result.process_id == pending_approval.predecessor_process_id
    assert steer_row == (None, None, None)
    assert public_event[:2] == (None, "action_resume_requested")
    assert predecessor_job == ("queued", None, None, None)
    assert predecessor_process == ("enqueued", None, None)
    assert runtime_count == (1, 1)
    assert running_attempt_count == 0

    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-2",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    claimed = job_payload_models.parse_action_job_payload_json(claimed["payload_json"])
    if decision == "denied":
        preparation = action_job_runtime_repository.ActionJobRuntimeRepository(
            db_path, BUSY_TIMEOUT_MS
        ).prepare_execution(payload=claimed, started_at="2026-08-16T00:00:23Z")
        assert preparation.context.approval_session_id == "approval-1"
        return

    original_prepare = (
        action_job_runtime_repository.ActionJobRuntimeRepository.prepare_execution
    )

    def prepare_then_stop(
        repository: action_job_runtime_repository.ActionJobRuntimeRepository,
        **kwargs: object,
    ) -> action_job_runtime_repository.ActionJobPreparation:
        preparation = original_prepare(repository, **kwargs)
        cancellation = ActionCancelRepository(
            db_path, BUSY_TIMEOUT_MS
        ).cancel_processing_action(
            user_id="user-1",
            action_id=action_id,
            completed_at="2026-08-16T00:00:30Z",
            process_completed_event_id="stop-after-prepare",
        )
        with pytest.raises(StopIteration) as completed:
            cancellation.send(None)
        assert completed.value.value.changed is True
        return preparation

    async def cancel_before_agent_start() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        action_job_runtime_repository.ActionJobRuntimeRepository,
        "prepare_execution",
        prepare_then_stop,
    )
    monkeypatch.setattr(
        "pantaray_agents.tasks.action_job_runtime.deps.get_action_application_service",
        cancel_before_agent_start,
    )

    with pytest.raises(asyncio.CancelledError):
        await _run_action_job(claimed)

    with sqlite3.connect(db_path) as connection:
        runtime_status = connection.execute(
            """
            SELECT jobs.status, processes.status
            FROM jobs
            JOIN processes ON processes.process_id = jobs.process_id
            WHERE jobs.job_id = ?
            """,
            (result.job_id,),
        ).fetchone()
        started_after_terminal = connection.execute(
            """
            SELECT COUNT(*)
            FROM process_events
            WHERE process_id = ? AND event_name = 'process_started'
              AND event_seq > (
                  SELECT MAX(event_seq)
                  FROM process_events
                  WHERE process_id = ? AND event_name = 'stream_end'
              )
            """,
            (result.process_id, result.process_id),
        ).fetchone()[0]

    assert runtime_status == ("canceled", "canceled")
    assert started_after_terminal == 0


def _add_second_pending_approval(fixture: PendingApprovalFixture) -> None:
    checkpoint = json.dumps(
        {
            "pending_approval_request": {
                "approval_session_id": "approval-1",
                "tool_request_id": "tool-request-1",
            },
            "current_approval_blockers": [
                {
                    "approval_session_id": "approval-1",
                    "tool_request_id": "tool-request-1",
                },
                {
                    "approval_session_id": "approval-2",
                    "tool_request_id": "tool-request-2",
                },
            ],
        }
    )
    pause_payload = json.dumps(
        {
            "action_id": fixture.action_id,
            "user_id": "user-1",
            "status": "processing",
            "reason": "approval_pending",
            "approval_blockers": [
                {
                    "approval_session_id": "approval-1",
                    "tool_request_id": "tool-request-1",
                },
                {
                    "approval_session_id": "approval-2",
                    "tool_request_id": "tool-request-2",
                },
            ],
        }
    )
    with sqlite3.connect(fixture.db_path) as connection:
        with connection:
            connection.execute(
                """
                UPDATE agent_action_steps
                SET runtime_state_checkpoint = ?
                WHERE action_id = ?
                """,
                (checkpoint, fixture.action_id),
            )
            connection.execute(
                """
                UPDATE process_events
                SET payload_json = ?
                WHERE process_id = ? AND event_name = 'process_paused'
                """,
                (pause_payload, fixture.predecessor_process_id),
            )
            connection.execute(
                """
                INSERT INTO approval_sessions(
                    approval_session_id, user_id, action_id, tool_id,
                    intent_class, approval_source, status,
                    approved_capabilities_json, command_summary_json,
                    requested_at, created_at, tool_request_id
                ) VALUES (
                    'approval-2', 'user-1', ?, 'tool-2', 'read_only',
                    'prompt', 'pending', '{"required_capabilities":[]}', '{}',
                    '2026-08-16T00:00:21Z', '2026-08-16T00:00:21Z',
                    'tool-request-2'
                )
                """,
                (fixture.action_id,),
            )


@pytest.mark.parametrize(
    (
        "selected_session_id",
        "selected_request_id",
        "remaining_session_id",
        "remaining_request_id",
    ),
    [
        ("approval-1", "tool-request-1", "approval-2", "tool-request-2"),
        ("approval-2", "tool-request-2", "approval-1", "tool-request-1"),
    ],
)
def test_parallel_approval_decisions_wait_for_each_new_pause(
    pending_approval: PendingApprovalFixture,
    selected_session_id: str,
    selected_request_id: str,
    remaining_session_id: str,
    remaining_request_id: str,
) -> None:
    _add_second_pending_approval(pending_approval)
    action_id = pending_approval.action_id
    first = apply_action_approval_decision(
        ActionApprovalDecisionCommand(
            user_id="user-1",
            action_id=action_id,
            process_id=pending_approval.predecessor_process_id,
            tool_request_id=selected_request_id,
            approval_session_id=selected_session_id,
            decision="approved_once",
        )
    )

    with pytest.raises(ActionApprovalDecisionError) as exc_info:
        apply_action_approval_decision(
            ActionApprovalDecisionCommand(
                user_id="user-1",
                action_id=action_id,
                process_id=pending_approval.predecessor_process_id,
                tool_request_id=remaining_request_id,
                approval_session_id=remaining_session_id,
                decision="approved_once",
            )
        )

    assert exc_info.value.error_code == "APPROVAL_DECISION_CONFLICT"
    with sqlite3.connect(pending_approval.db_path) as connection:
        statuses = dict(
            connection.execute(
                """
                SELECT approval_session_id, status
                FROM approval_sessions
                ORDER BY approval_session_id
                """
            ).fetchall()
        )
        queued_count = connection.execute(
            """
            SELECT COUNT(*) FROM jobs
            WHERE logical_key = ? AND status = 'queued'
            """,
            (action_id,),
        ).fetchone()[0]
    assert statuses[selected_session_id] == "approved_once"
    assert statuses[remaining_session_id] == "pending"
    assert queued_count == 1

    claimed = claim_next_pending_job(
        db_path=str(pending_approval.db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-resume",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None and claimed["job_id"] == first.job_id
    mark_local_action_job_paused(
        db_path=pending_approval.db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_id=first.job_id,
        process_id=first.process_id,
        payload={
            "action_id": action_id,
            "user_id": "user-1",
            "status": "processing",
            "completed_at": "2026-08-16T00:00:22Z",
            "reason": "approval_pending",
            "approval_blockers": [
                {
                    "approval_session_id": remaining_session_id,
                    "tool_request_id": remaining_request_id,
                }
            ],
        },
    )
    second = apply_action_approval_decision(
        ActionApprovalDecisionCommand(
            user_id="user-1",
            action_id=action_id,
            process_id=pending_approval.predecessor_process_id,
            tool_request_id=remaining_request_id,
            approval_session_id=remaining_session_id,
            decision="approved_once",
        )
    )

    with sqlite3.connect(pending_approval.db_path) as connection:
        final_statuses = dict(
            connection.execute(
                "SELECT approval_session_id, status FROM approval_sessions"
            ).fetchall()
        )
        runtime_status = connection.execute(
            """
            SELECT jobs.status, processes.status
            FROM jobs JOIN processes ON processes.process_id = jobs.process_id
            WHERE jobs.job_id = ?
            """,
            (first.job_id,),
        ).fetchone()
        resume_edges = connection.execute(
            """
            SELECT process_id,
                   json_extract(payload_json, '$.predecessor_process_id'),
                   json_extract(payload_json, '$.approval_session_id')
            FROM process_events
            WHERE event_name = 'action_resume_requested'
            ORDER BY event_seq
            """
        ).fetchall()
    assert final_statuses == {
        "approval-1": "approved_once",
        "approval-2": "approved_once",
    }
    assert first.job_id == second.job_id == pending_approval.predecessor_job_id
    assert (
        first.process_id
        == second.process_id
        == (pending_approval.predecessor_process_id)
    )
    assert runtime_status == ("queued", "enqueued")
    assert set(resume_edges) == {
        (first.process_id, first.process_id, "approval-1"),
        (first.process_id, first.process_id, "approval-2"),
    }


def test_action_approval_mismatch_rolls_back_decision_and_resume_job(
    pending_approval: PendingApprovalFixture,
) -> None:
    db_path = pending_approval.db_path
    action_id = pending_approval.action_id

    with pytest.raises(
        ActionApprovalDecisionError,
        match="does not match",
    ) as exc_info:
        apply_action_approval_decision(
            ActionApprovalDecisionCommand(
                user_id="user-1",
                action_id=action_id,
                process_id=pending_approval.predecessor_process_id,
                tool_request_id="tool-request-1",
                approval_session_id="different-approval",
                decision="approved_once",
            )
        )

    assert exc_info.value.error_code == "APPROVAL_REQUEST_MISMATCH"
    with sqlite3.connect(db_path) as connection:
        approval_status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id = 'approval-1'"
        ).fetchone()
        resume_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE logical_key = ? AND status = 'queued'",
            (action_id,),
        ).fetchone()[0]
    assert approval_status == ("pending",)
    assert resume_jobs == 0


def test_action_approval_rejects_a_different_process_without_mutation(
    pending_approval: PendingApprovalFixture,
) -> None:
    with pytest.raises(ActionApprovalDecisionError) as exc_info:
        apply_action_approval_decision(
            ActionApprovalDecisionCommand(
                user_id="user-1",
                action_id=pending_approval.action_id,
                process_id="different-process",
                tool_request_id="tool-request-1",
                approval_session_id="approval-1",
                decision="approved_once",
            )
        )

    assert exc_info.value.error_code == "APPROVAL_INTERRUPTED"
    with sqlite3.connect(pending_approval.db_path) as connection:
        approval_status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id = 'approval-1'"
        ).fetchone()
        process_status = connection.execute(
            "SELECT status FROM processes WHERE process_id = ?",
            (pending_approval.predecessor_process_id,),
        ).fetchone()
    assert approval_status == ("pending",)
    assert process_status == ("paused",)


def _ask_about_outside_folder(
    pending_approval: PendingApprovalFixture, folder: Path
) -> str:
    """Bind approval-1 to the Action's manifest as an outside-workspace ask."""

    db_path = pending_approval.db_path
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    manifest_id = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=pending_approval.action_id,
        started_at="2026-08-16T00:00:10Z",
        allowed_tool_ids=("apply_patch",),
    ).manifest_id
    summary = build_apply_patch_summary(
        patch_paths=(str(folder / "notes.txt"),),
        outside_workspace_folder=folder,
        outside_workspace_grantable=True,
    )
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE approval_sessions SET manifest_id = ?, command_summary_json = ? "
            "WHERE approval_session_id = 'approval-1'",
            (manifest_id, json.dumps(summary)),
        )
    return manifest_id


def _conversation_decision(
    pending_approval: PendingApprovalFixture,
) -> ActionApprovalDecisionCommand:
    return ActionApprovalDecisionCommand(
        user_id="user-1",
        action_id=pending_approval.action_id,
        process_id=pending_approval.predecessor_process_id,
        tool_request_id="tool-request-1",
        approval_session_id="approval-1",
        decision="approved_for_conversation",
    )


def _approved_folder_roots(db_path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT manifest_id, source_id, canonical_real_path "
            "FROM workspace_manifest_roots WHERE source_type = 'approved_folder'"
        ).fetchall()


def test_conversation_approval_resumes_and_grants_the_folder(
    pending_approval: PendingApprovalFixture,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    folder = tmp_path_factory.mktemp("outside-folder").resolve()
    manifest_id = _ask_about_outside_folder(pending_approval, folder)

    result = apply_action_approval_decision(_conversation_decision(pending_approval))
    with pytest.raises(ActionApprovalDecisionError) as replay:
        apply_action_approval_decision(_conversation_decision(pending_approval))

    assert result.decision == "approved_for_conversation"
    assert replay.value.error_code == "APPROVAL_DECISION_ALREADY_APPLIED"
    with sqlite3.connect(pending_approval.db_path) as connection:
        approval_status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id = 'approval-1'"
        ).fetchone()
    assert approval_status == ("approved_once",)
    assert _approved_folder_roots(pending_approval.db_path) == [
        (manifest_id, "approval-1", str(folder))
    ]


def test_conversation_approval_is_refused_without_a_grantable_folder(
    pending_approval: PendingApprovalFixture,
) -> None:
    command = _conversation_decision(pending_approval)
    # approval-1 first asks about no folder, then about a folder enclosing the
    # app database.
    rejections = []
    with pytest.raises(ActionApprovalDecisionError) as not_outside:
        apply_action_approval_decision(command)
    rejections.append(not_outside.value.error_code)
    _ask_about_outside_folder(pending_approval, pending_approval.db_path.parent.parent)
    with pytest.raises(ActionApprovalDecisionError) as enclosing:
        apply_action_approval_decision(command)
    rejections.append(enclosing.value.error_code)
    with pytest.raises(ValidationError):
        command.model_validate({**command.model_dump(), "decision": "always"})

    assert rejections == ["APPROVAL_REQUEST_MISMATCH"] * 2
    with sqlite3.connect(pending_approval.db_path) as connection:
        approval_status = connection.execute(
            "SELECT status FROM approval_sessions WHERE approval_session_id = 'approval-1'"
        ).fetchone()
        resume_jobs = connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE logical_key = ? AND status = 'queued'",
            (pending_approval.action_id,),
        ).fetchone()[0]
    assert approval_status == ("pending",)
    assert resume_jobs == 0
    assert _approved_folder_roots(pending_approval.db_path) == []


@pytest.mark.asyncio
async def test_action_approval_resume_uses_the_checkpoint_owning_followup_user_step(
    pending_approval: PendingApprovalFixture,
) -> None:
    db_path = pending_approval.db_path
    action_id = pending_approval.action_id
    followup = ActionUserMessageInput(
        message_id="message-2",
        content="Continue the second turn after approval",
    )
    checkpoint = json.dumps(
        {
            "pending_approval_request": {
                "approval_session_id": "approval-1",
                "tool_request_id": "tool-request-1",
            },
            "current_approval_blockers": [],
        }
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                UPDATE agent_action_steps
                SET runtime_state_checkpoint = NULL,
                    runtime_state_checkpoint_version = NULL
                WHERE action_id = ?
                """,
                (action_id,),
            )
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, parent_step_id, step_number,
                    local_step_number, short_step_id, step_type, step_name, status,
                    goal_handle, retry_count, prompt_tokens, completion_tokens,
                    user_message_id, user_message_json, user_request_text,
                    runtime_state_checkpoint, runtime_state_checkpoint_version,
                    accepted_sequence, adopted_process_id,
                    started_at, completed_at, created_at
                ) VALUES (
                    'user-step-2', ?, 'user-1', NULL, 2, 2, 'S-2-USER',
                    'user_request', 'user_request', 'success', 'S', 0, 0, 0,
                    'message-2', ?, ?, ?, 1, 2, ?, ?, ?, ?
                )
                """,
                (
                    action_id,
                    serialize_action_user_message(followup),
                    followup.content,
                    checkpoint,
                    pending_approval.predecessor_process_id,
                    "2026-08-16T00:01:00Z",
                    "2026-08-16T00:01:00Z",
                    "2026-08-16T00:01:00Z",
                ),
            )

    result = apply_action_approval_decision(
        ActionApprovalDecisionCommand(
            user_id="user-1",
            action_id=action_id,
            process_id=pending_approval.predecessor_process_id,
            tool_request_id="tool-request-1",
            approval_session_id="approval-1",
            decision="approved_once",
        )
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        payload = json.loads(
            connection.execute(
                """
                SELECT payload
                FROM agent_process_events
                WHERE action_id = ? AND event_name = 'action_resume_requested'
                """,
                (action_id,),
            ).fetchone()[0]
        )
        continuation = json.loads(
            connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id = ?",
                (result.job_id,),
            ).fetchone()[0]
        )["continuation_ref"]
        lineage = resolve_action_process_lineage_in_connection(
            connection=connection,
            user_id="user-1",
            action_id=action_id,
            process_id=result.process_id,
        )

    assert payload["data"]["command_id"] == "message-2"
    assert payload["data"]["accepted_at"] == "2026-08-16T00:01:00Z"
    assert continuation == {
        "kind": "tool_approval",
        "approval_session_id": "approval-1",
        "tool_request_id": "tool-request-1",
    }
    assert lineage.root_process_id == pending_approval.predecessor_process_id

    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-2",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    message = ActionUserMessageInput(message_id="message-3", content="Continue")
    pending = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=ExistingActionTarget(
                action_id=action_id,
                expected_process_id=pending_approval.predecessor_process_id,
            ),
            message=message,
        )
    )
    assert pending.disposition == "pending"

    state = create_initial_state(
        user_id="user-1",
        suggestion_id=None,
        action_id=action_id,
        started_at="2026-08-16T00:00:00Z",
        max_steps=25,
        max_tool_steps=25,
        token_budget=None,
    )
    for step_id, step_number, text in (
        (pending_approval.user_step_id, 1, "Continue after tool approval"),
        ("user-step-2", 2, followup.content),
    ):
        state = project_persisted_user_request_step(
            state,
            step_id=step_id,
            step_number=step_number,
            local_step_number=step_number,
            short_step_id=f"S-{step_number}-USER",
            request_text=text,
            occurred_at=f"2026-08-16T00:0{step_number}:00Z",
            history_phase="executing",
        )
    state["phase"] = "executing"
    request = ActionAgentRequest(
        user_id="user-1",
        suggestion_id=None,
        action_id=action_id,
        user_step_id="user-step-2",
        user_step_number=2,
        user_step_local_step_number=2,
        user_step_short_id="S-2-USER",
        user_step_created_at="2026-08-16T00:01:00Z",
        user_message=followup,
        approval_resume_session_id="approval-1",
        approval_resume_tool_request_id="tool-request-1",
    )
    adopt_pending_action_user_steps_at_parent_think(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_id=result.job_id,
        process_id=result.process_id,
        request=request,
        state=state,
    )

    checkpoint_row = await build_action_repository(
        db_path
    ).get_runtime_checkpoint_for_approval_resume(
        user_id="user-1",
        action_id=action_id,
        approval_session_id="approval-1",
        tool_request_id="tool-request-1",
    )
    resolved = ActionResumeService(
        ResumeDeps(
            logger=logging.getLogger(__name__),
            load_approval_session_by_request=lambda _user_id, _request_id: None,
            build_agent_error=lambda **values: AgentError(severity="error", **values),
            now_provider=lambda: "2026-08-16T00:03:00Z",
        )
    ).resolve_initial_state(
        request=request,
        started_at="2026-08-16T00:03:00Z",
        state_config={
            "max_steps": 25,
            "max_tool_steps": 25,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        token_budget=None,
        checkpoint_row=checkpoint_row,
    )

    history = [
        entry for entries in resolved["history_by_scope"].values() for entry in entries
    ]
    assert checkpoint_row.metadata == {"approval_anchor_step_id": "user-step-2"}
    assert [entry["step_id"] for entry in history].count(pending.user_step_id) == 1
    assert "pending_approval_request" not in resolved

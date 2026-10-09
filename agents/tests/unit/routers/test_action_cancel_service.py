from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from pantaray_agents.local_runtime.runtime.action_approval import (
    ActionApprovalDecisionCommand,
    ActionApprovalDecisionError,
    apply_action_approval_decision,
)
from pantaray_agents.local_runtime.runtime.action_cancel_repository import (
    ActionCancelNotFoundError,
    ActionCancelResult,
    ActionCancelStateConflictError,
)
from pantaray_agents.local_runtime.runtime.action_job_runtime_repository import (
    ActionJobRuntimeRepository,
)
from pantaray_agents.local_runtime.runtime.action_messages import (
    ExistingActionTarget,
    NewActionTarget,
    SubmitActionMessageCommand,
    SubmitActionMessageResult,
    submit_action_message,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.job_queue_runtime import (
    claim_next_pending_action_job,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ExecutionSessionCreateInput,
    StoredToolRuntimeResource,
    ToolInvocationStartInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    complete_execution_session,
    create_execution_session,
    ensure_action_workspace_manifest,
    record_tool_invocation_start,
)
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    ActionCleanupPassResult,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    create_tool_runtime_resource,
)
from pantaray_agents.schema.agent.action import (
    ActionUserMessageInput,
    SuggestionApprovalInput,
)

BUSY_TIMEOUT_MS = 1_000


@pytest.fixture(autouse=True)
def _reset_owner_state() -> None:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()
    reset_logged_out_owner()


def _bootstrap_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", str(BUSY_TIMEOUT_MS))
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )
    return db_path


def _submit_and_start_turn(
    *,
    db_path: Path,
    action_id: str | None = None,
    message_id: str,
    content: str,
) -> SubmitActionMessageResult:
    target = (
        ExistingActionTarget(action_id=action_id, expected_process_id=None)
        if action_id is not None
        else NewActionTarget()
    )
    result = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=target,
            message=ActionUserMessageInput(
                message_id=message_id,
                content=content,
            ),
        )
    )
    payload = claim_next_pending_action_job(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        owner_user_id="user-1",
        claimed_by=f"worker:{message_id}",
    )
    assert payload is not None
    preparation = ActionJobRuntimeRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).prepare_execution(
        payload=payload,
        started_at=f"2026-08-16T00:00:0{message_id[-1]}Z",
    )
    assert preparation.skip_outcome is None
    return result


def _running_invocation(
    *,
    invocation_id: str,
    tool_request_id: str,
    action_id: str,
    step_id: str,
    manifest_id: str,
    execution_session_id: str,
    started_at: str,
) -> ToolInvocationStartInput:
    return ToolInvocationStartInput(
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
        user_id="user-1",
        action_id=action_id,
        step_id=step_id,
        tool_id="bash",
        manifest_id=manifest_id,
        execution_session_id=execution_session_id,
        cwd=".",
        timeout_ms=5_000,
        intent_class="process_exec_local",
        network_policy="cloud-proxy-only",
        command_summary_json={"summary_kind": "bash", "command": "sleep 30"},
        capability_snapshot_json={"required_capabilities": ["process_exec_local"]},
        request_json={"args": {"command": "sleep 30"}},
        status="running",
        started_at=started_at,
    )


def _finish_turn_for_followup(
    *, db_path: Path, turn: SubmitActionMessageResult
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_action_steps
            SET runtime_state_checkpoint = '{}', runtime_state_checkpoint_version = 4
            WHERE step_id = ?
            """,
            (turn.user_step_id,),
        )
        connection.execute(
            "UPDATE agent_actions SET status = 'success' WHERE action_id = ?",
            (turn.action_id,),
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE job_id = ?", (turn.job_id,)
        )
        connection.execute(
            "UPDATE processes SET status = 'completed', current_job_id = NULL "
            "WHERE process_id = ?",
            (turn.process_id,),
        )
        connection.execute(
            "UPDATE job_attempts SET status = 'completed' WHERE job_id = ?",
            (turn.job_id,),
        )


@pytest.mark.asyncio
async def test_queued_action_cancel_finishes_before_worker_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    turn = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=NewActionTarget(),
            message=ActionUserMessageInput(
                message_id="message-1",
                content="Cancel before the worker starts",
            ),
        )
    )

    assert await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
        expected_process_id=turn.process_id,
    )

    with sqlite3.connect(db_path) as connection:
        action_status = connection.execute(
            "SELECT status FROM agent_actions WHERE action_id = ?",
            (turn.action_id,),
        ).fetchone()
        job_status = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (turn.job_id,)
        ).fetchone()
        process_status = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            (turn.process_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT event_name, payload FROM agent_process_events WHERE action_id = ?",
            (turn.action_id,),
        ).fetchall()
        worker_start = connection.execute(
            "SELECT 1 FROM process_events "
            "WHERE process_id = ? AND event_name = 'process_started'",
            (turn.process_id,),
        ).fetchone()

    assert action_status == ("canceled",)
    assert job_status == ("canceled",)
    assert process_status == ("canceled", None)
    assert [
        json.loads(str(payload))["data"]["command_id"]
        for event_name, payload in events
        if event_name == "process_completed"
    ] == ["message-1"]
    assert worker_start is None


@pytest.mark.asyncio
async def test_cancel_rejects_stale_process_fence_without_mutating_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    prior = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Finish the first run",
    )
    _finish_turn_for_followup(db_path=db_path, turn=prior)
    successor = _submit_and_start_turn(
        db_path=db_path,
        action_id=prior.action_id,
        message_id="message-2",
        content="Start the successor run",
    )
    assert successor.process_id != prior.process_id

    def runtime_snapshot() -> tuple[list[tuple[object, ...]], int, int]:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                """
                SELECT action.status, process.process_id, process.status,
                       process.current_job_id, process.terminal_event_id,
                       job.job_id, job.status
                FROM agent_actions AS action
                JOIN processes AS process ON process.action_id = action.action_id
                JOIN jobs AS job ON job.process_id = process.process_id
                WHERE action.action_id = ?
                ORDER BY process.process_id, job.job_id
                """,
                (prior.action_id,),
            ).fetchall()
            internal_event_count = connection.execute(
                """SELECT COUNT(*) FROM process_events
                   WHERE process_id IN (?, ?)""",
                (prior.process_id, successor.process_id),
            ).fetchone()[0]
            public_event_count = connection.execute(
                """SELECT COUNT(*) FROM agent_process_events
                   WHERE action_id = ?""",
                (prior.action_id,),
            ).fetchone()[0]
        return rows, int(internal_event_count), int(public_event_count)

    before = runtime_snapshot()
    with pytest.raises(HTTPException) as exc_info:
        await cancel_service.execute_action_cancel(
            user_id="user-1",
            action_id=prior.action_id,
            reason="user_stop",
            expected_process_id=prior.process_id,
        )

    assert exc_info.value.status_code == 409
    assert runtime_snapshot() == before


@pytest.mark.asyncio
async def test_standalone_action_cancel_uses_action_owned_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Cancel this standalone Action",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO approval_sessions(
                approval_session_id, user_id, action_id, manifest_id,
                tool_request_id, tool_invocation_id, tool_id, intent_class,
                approval_source, status, approved_capabilities_json,
                command_summary_json, requested_at, decided_at, created_at,
                claimed_at
            ) VALUES (
                'approval-1', 'user-1', ?, NULL, 'tool-request-1', NULL,
                'bash', 'process_exec_local', 'prompt', 'pending', '{}', '{}',
                '2026-08-16T00:00:02Z', NULL, '2026-08-16T00:00:02Z', NULL
            )
            """,
            (turn.action_id,),
        )
    await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
    )
    with pytest.raises(ActionApprovalDecisionError) as exc_info:
        apply_action_approval_decision(
            ActionApprovalDecisionCommand(
                user_id="user-1",
                action_id=turn.action_id,
                process_id=turn.process_id,
                tool_request_id="tool-request-1",
                approval_session_id="approval-1",
                decision="approved_once",
            )
        )
    assert exc_info.value.error_code == "APPROVAL_DECISION_CONFLICT"

    with sqlite3.connect(db_path) as connection:
        action_row = connection.execute(
            "SELECT suggestion_id, status FROM agent_actions WHERE action_id = ?",
            (turn.action_id,),
        ).fetchone()
        job_row = connection.execute(
            "SELECT status FROM jobs WHERE job_id = ?",
            (turn.job_id,),
        ).fetchone()
        process_row = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            (turn.process_id,),
        ).fetchone()
        public_payload = connection.execute(
            """
            SELECT payload
            FROM agent_process_events
            WHERE action_id = ? AND event_name = 'process_completed'
            """,
            (turn.action_id,),
        ).fetchone()
        approval_row = connection.execute(
            """
            SELECT status, decided_at
            FROM approval_sessions
            WHERE approval_session_id = 'approval-1'
            """
        ).fetchone()

    assert action_row == (None, "canceled")
    assert job_row == ("canceled",)
    assert process_row == ("canceled", None)
    assert approval_row is not None
    assert approval_row[0] == "interrupted"
    assert approval_row[1] is not None
    assert public_payload is not None
    event_data = json.loads(str(public_payload[0]))["data"]
    assert event_data["command_id"] == "message-1"
    assert event_data["action_id"] == turn.action_id
    assert "suggestion_id" not in event_data


@pytest.mark.asyncio
async def test_cancel_marks_pending_and_late_user_messages_not_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Stop this run",
    )
    pending = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=ExistingActionTarget(
                action_id=turn.action_id,
                expected_process_id=turn.process_id,
            ),
            message=ActionUserMessageInput(
                message_id="message-2",
                content="Queued before Stop",
            ),
        )
    )
    assert pending.disposition == "pending"

    await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
    )
    late = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=ExistingActionTarget(
                action_id=turn.action_id,
                expected_process_id=turn.process_id,
            ),
            message=ActionUserMessageInput(
                message_id="message-3",
                content="Submitted after Stop",
            ),
        )
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT user_message_id, expected_process_id, adoption_canceled_at,
                   adopted_process_id, step_number
            FROM agent_action_steps
            WHERE user_message_id IN ('message-2', 'message-3')
            ORDER BY accepted_sequence
            """
        ).fetchall()
    assert late.disposition == "not_executed"
    assert [row[:2] for row in rows] == [
        ("message-2", turn.process_id),
        ("message-3", turn.process_id),
    ]
    assert all(row[2] is not None and row[3:] == (None, None) for row in rows)


@pytest.mark.asyncio
async def test_blocked_action_job_is_not_cancelable_as_active_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Do not cancel a blocked runtime",
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'blocked', completed_at = '2026-08-16T00:00:02Z',
                    error_code = 'LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR'
                WHERE job_id = ?
                """,
                (turn.job_id,),
            )
            connection.execute(
                """
                UPDATE job_attempts
                SET status = 'failed', completed_at = '2026-08-16T00:00:02Z',
                    error_code = 'LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR'
                WHERE job_id = ?
                """,
                (turn.job_id,),
            )

    with pytest.raises(HTTPException) as exc_info:
        await cancel_service.execute_action_cancel(
            user_id="user-1",
            action_id=turn.action_id,
            reason="user_stop",
        )

    with sqlite3.connect(db_path) as connection:
        action = connection.execute(
            "SELECT status FROM agent_actions WHERE action_id = ?",
            (turn.action_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id = ?",
            (turn.job_id,),
        ).fetchone()
        process = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            (turn.process_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT status, error_code FROM job_attempts WHERE job_id = ?",
            (turn.job_id,),
        ).fetchone()

    assert exc_info.value.status_code == 409
    assert action == ("processing",)
    assert job == ("blocked", "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR")
    assert process == ("running", turn.job_id)
    assert attempt == ("failed", "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR")


@pytest.mark.asyncio
async def test_followup_cancel_uses_latest_user_turn_not_initial_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    first = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="First turn",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_action_steps
            SET runtime_state_checkpoint = '{}',
                runtime_state_checkpoint_version = 4
            WHERE step_id = ?
            """,
            (first.user_step_id,),
        )
        connection.execute(
            "UPDATE agent_actions SET status = 'success' WHERE action_id = ?",
            (first.action_id,),
        )
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE job_id = ?",
            (first.job_id,),
        )
        connection.execute(
            """
            UPDATE processes
            SET status = 'completed', current_job_id = NULL
            WHERE process_id = ?
            """,
            (first.process_id,),
        )
        connection.execute(
            "UPDATE job_attempts SET status = 'completed' WHERE job_id = ?",
            (first.job_id,),
        )
    second = _submit_and_start_turn(
        db_path=db_path,
        action_id=first.action_id,
        message_id="message-2",
        content="Second turn",
    )

    await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=first.action_id,
        reason=None,
    )

    with sqlite3.connect(db_path) as connection:
        payloads = connection.execute(
            """
            SELECT payload
            FROM agent_process_events
            WHERE action_id = ? AND event_name = 'process_completed'
            ORDER BY sequence
            """,
            (first.action_id,),
        ).fetchall()
        user_steps = connection.execute(
            """
            SELECT short_step_id, user_message_id
            FROM agent_action_steps
            WHERE action_id = ? AND step_type = 'user_request'
            ORDER BY step_number
            """,
            (first.action_id,),
        ).fetchall()

    assert second.disposition == "started"
    assert [json.loads(str(row[0]))["data"]["command_id"] for row in payloads] == [
        "message-2"
    ]
    assert user_steps == [("S-1-USER", "message-1"), ("S-2-USER", "message-2")]


@pytest.mark.asyncio
async def test_suggestion_link_is_only_an_optional_cancel_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, has_suggestion,
                interaction_contract, answer, suggestion_summary,
                target_context_json, created_at, updated_at
            ) VALUES (
                'suggestion-1', 'user-1', 'success', 1, 'action_offer',
                'Apply the approved change', 'Keep it narrow', '{}',
                '2026-08-16T00:00:00Z', '2026-08-16T00:00:00Z'
            )
            """
        )
    turn = submit_action_message(
        SubmitActionMessageCommand(
            user_id="user-1",
            target=NewActionTarget(suggestion_id="suggestion-1"),
            message=ActionUserMessageInput(
                message_id="message-approval-1",
                content="Apply the approved change",
                suggestion_approval=SuggestionApprovalInput(
                    suggestion_id="suggestion-1",
                    approved_at="2026-08-16T00:00:01Z",
                    summary="Keep it narrow",
                ),
            ),
        )
    )
    payload = claim_next_pending_action_job(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        owner_user_id="user-1",
        claimed_by="worker:suggestion",
    )
    assert payload is not None
    ActionJobRuntimeRepository(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    ).prepare_execution(payload=payload, started_at="2026-08-16T00:00:02Z")

    await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason=None,
    )

    with sqlite3.connect(db_path) as connection:
        suggestion_row = connection.execute(
            """
            SELECT action_status, action_command_id
            FROM agent_suggestions
            WHERE suggestion_id = 'suggestion-1'
            """
        ).fetchone()
        action_row = connection.execute(
            "SELECT suggestion_id, status FROM agent_actions WHERE action_id = ?",
            (turn.action_id,),
        ).fetchone()
    assert suggestion_row == ("canceled", "message-approval-1")
    assert action_row == ("suggestion-1", "canceled")


@pytest.mark.asyncio
async def test_cancel_cleanup_stays_on_committed_session_when_next_session_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    prior_turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Finish despite a session completion write failure",
    )
    stale_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=prior_turn.action_id,
        started_at="2026-08-16T00:00:10Z",
        allowed_tool_ids=("bash",),
    )
    stale_session_temp = stale_context.action_temp_dir
    with sqlite3.connect(db_path) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            complete_execution_session(
                db_path=db_path,
                busy_timeout_ms=1,
                execution_session_id=stale_context.execution_session_id,
                status="completed",
                completed_at="2026-08-16T00:00:11Z",
            )
        blocker.rollback()
    _finish_turn_for_followup(db_path=db_path, turn=prior_turn)
    turn = _submit_and_start_turn(
        db_path=db_path,
        action_id=prior_turn.action_id,
        message_id="message-2",
        content="Run a cancellable tool in the follow-up",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=turn.action_id,
        started_at="2026-08-16T00:00:20Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    canonical_session_temp = context.action_temp_dir
    descendant_session_id = "descendant-session"
    descendant_session_temp = context.action_temp_dir.with_name(descendant_session_id)
    descendant_session_temp.mkdir()
    create_execution_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        session=ExecutionSessionCreateInput(
            execution_session_id=descendant_session_id,
            user_id="user-1",
            action_id=turn.action_id,
            parent_execution_session_id=context.execution_session_id,
            exec_mode="brokered_file_ops",
            cwd_path=str(context.workspace_path),
            action_temp_dir=str(descendant_session_temp),
            app_runtime_python=str(context.app_runtime_python),
            network_policy=context.network_policy,
            read_access_scope=context.read_access_scope,
            capability_snapshot_json={},
            tool_allowlist_json=["bash"],
            status="running",
            started_at="2026-08-16T00:00:21Z",
            expires_at=None,
        ),
    )
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-descendant-root",
            execution_session_id=descendant_session_id,
            tool_invocation_id=None,
            action_id=turn.action_id,
            resource_kind="temp_dir",
            status="active",
            created_at="2026-08-16T00:00:21Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(descendant_session_temp),
        ),
    )
    ensure_action_workspace_manifest(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=turn.action_id,
        execution_session_id=descendant_session_id,
        workspace_path=context.workspace_path,
        tool_results_path=context.tool_results_path,
        artifact_root=tmp_path / "artifacts",
        created_at="2026-08-16T00:00:21Z",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_action_steps
            SET runtime_state_checkpoint = '{}', runtime_state_checkpoint_version = 4
            WHERE step_id = ?
            """,
            (turn.user_step_id,),
        )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=_running_invocation(
            invocation_id="invocation-1",
            tool_request_id="request-1",
            action_id=turn.action_id,
            step_id=turn.user_step_id,
            manifest_id=context.manifest_id,
            execution_session_id=descendant_session_id,
            started_at="2026-08-16T00:00:22Z",
        ),
    )
    temp_file = descendant_session_temp / "cancel-temp.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    child_process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_cleanup."
        "classify_process_identity",
        lambda **_kwargs: "match",
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_cleanup."
        "process_exists",
        lambda _pid: child_process.poll() is None,
    )
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-process-group",
            execution_session_id=descendant_session_id,
            tool_invocation_id="invocation-1",
            action_id=turn.action_id,
            resource_kind="process_group",
            status="active",
            created_at="2026-08-16T00:00:22Z",
            pid=child_process.pid,
            pgid=os.getpgid(child_process.pid),
            process_start_signature="process-start-signature",
            resource_path=None,
        ),
    )
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-file",
            execution_session_id=descendant_session_id,
            tool_invocation_id="invocation-1",
            action_id=turn.action_id,
            resource_kind="temp_file",
            status="active",
            created_at="2026-08-16T00:00:22Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(temp_file),
        ),
    )
    original_cleanup_pass = cancel_service._run_cancel_cleanup_pass
    new_context: ActionExecutionContext | None = None
    observed_terminal_rows: list[tuple[str, str]] = []

    async def start_next_session_before_cleanup(
        *,
        db_path: Path,
        busy_timeout_ms: int,
        action_id: str,
        execution_session_id: str,
        suggestion_id: str | None,
    ) -> ActionCleanupPassResult:
        nonlocal new_context
        if new_context is None:
            _submit_and_start_turn(
                db_path=db_path,
                action_id=action_id,
                message_id="message-3",
                content="Continue after cancel",
            )
            new_context = ensure_action_scratch_execution_context(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id="user-1",
                action_id=action_id,
                started_at="2026-08-16T00:00:30Z",
                allowed_tool_ids=("bash",),
            )
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                """
                SELECT action.status, session.status
                FROM agent_actions AS action
                JOIN execution_sessions AS session
                  ON session.action_id = action.action_id
                WHERE action.action_id = ? AND session.execution_session_id = ?
                """,
                (turn.action_id, context.execution_session_id),
            ).fetchone()
        assert row is not None
        observed_terminal_rows.append((str(row[0]), str(row[1])))
        return await original_cleanup_pass(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            action_id=action_id,
            execution_session_id=execution_session_id,
            suggestion_id=suggestion_id,
        )

    monkeypatch.setattr(
        cancel_service,
        "_run_cancel_cleanup_pass",
        start_next_session_before_cleanup,
    )

    try:
        await cancel_service.execute_action_cancel(
            user_id="user-1",
            action_id=turn.action_id,
            reason=None,
        )
    finally:
        if child_process.poll() is None:
            child_process.kill()
            child_process.wait(timeout=5)

    with sqlite3.connect(db_path) as connection:
        invocation_status = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = 'invocation-1'"
        ).fetchone()
        resource_rows = connection.execute(
            """
            SELECT execution_session_id, resource_kind, status
            FROM tool_runtime_resources
            """
        ).fetchall()
        session_rows = connection.execute(
            """
            SELECT execution_session_id, status FROM execution_sessions
            WHERE execution_session_id IN (?, ?) ORDER BY execution_session_id
            """,
            (context.execution_session_id, descendant_session_id),
        ).fetchall()
    assert child_process.poll() is not None
    assert set(observed_terminal_rows) == {("processing", "canceled")}
    assert invocation_status == ("canceled",)
    assert set(session_rows) == {
        (descendant_session_id, "canceled"),
        (context.execution_session_id, "canceled"),
    }
    assert new_context is not None
    assert set(resource_rows) == {
        (stale_context.execution_session_id, "temp_dir", "cleaned"),
        (context.execution_session_id, "temp_dir", "cleaned"),
        (descendant_session_id, "process_group", "cleaned"),
        (descendant_session_id, "temp_dir", "cleaned"),
        (descendant_session_id, "temp_file", "cleaned"),
        (new_context.execution_session_id, "temp_dir", "active"),
    }
    assert not stale_session_temp.exists()
    assert not canonical_session_temp.exists()
    assert not descendant_session_temp.exists()
    assert new_context.action_temp_dir.exists()
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """DELETE FROM tool_outputs WHERE invocation_id = 'invocation-1';
            UPDATE tool_invocations SET status = 'running', completed_at = NULL
            WHERE invocation_id = 'invocation-1';"""
        )
    assert await cancel_service.execute_action_cancel(
        user_id="user-1", action_id=turn.action_id, reason=None
    )
    assert not new_context.action_temp_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_followup", [False, True])
async def test_cancel_cleans_completed_session_across_action_terminal_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_followup: bool,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Stop after execution finishes",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=turn.action_id,
        started_at="2026-08-16T00:00:10Z",
        allowed_tool_ids=("bash",),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=_running_invocation(
            invocation_id="invocation-completed-session",
            tool_request_id="request-completed-session",
            action_id=turn.action_id,
            step_id=turn.user_step_id,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-08-16T00:00:11Z",
        ),
    )
    temp_file = context.action_temp_dir / "completed-session.tmp"
    temp_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-completed-session",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-completed-session",
            action_id=turn.action_id,
            resource_kind="temp_file",
            status="active",
            created_at="2026-08-16T00:00:12Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(temp_file),
        ),
    )
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-08-16T00:00:13Z",
    )
    followup_context: ActionExecutionContext | None = None
    if start_followup:
        _finish_turn_for_followup(db_path=db_path, turn=turn)
        followup = _submit_and_start_turn(
            db_path=db_path,
            action_id=turn.action_id,
            message_id="message-2",
            content="Stop after starting the follow-up",
        )
        followup_context = ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            action_id=followup.action_id,
            started_at="2026-08-16T00:00:20Z",
            allowed_tool_ids=("bash",),
        )

    assert await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason="user_stop",
    )

    with sqlite3.connect(db_path) as connection:
        session_status = connection.execute(
            "SELECT status FROM execution_sessions WHERE execution_session_id = ?",
            (context.execution_session_id,),
        ).fetchone()
        invocation_status = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            ("invocation-completed-session",),
        ).fetchone()
        resource_statuses = connection.execute(
            """
            SELECT resource_kind, status
            FROM tool_runtime_resources
            WHERE execution_session_id = ?
            ORDER BY resource_kind
            """,
            (context.execution_session_id,),
        ).fetchall()
    assert session_status == ("completed",)
    assert invocation_status == ("canceled",)
    assert resource_statuses == [("temp_dir", "cleaned"), ("temp_file", "cleaned")]
    assert not context.action_temp_dir.exists()
    if followup_context is not None:
        assert not followup_context.action_temp_dir.exists()


@pytest.mark.asyncio
async def test_canceled_replay_converges_interrupted_post_commit_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.resources import cancel_cleanup
    from pantaray_agents.routers import action_cancel_service as cancel_service

    db_path = _bootstrap_runtime(tmp_path, monkeypatch)
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    turn = _submit_and_start_turn(
        db_path=db_path,
        message_id="message-1",
        content="Retry interrupted cleanup after cancel",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id=turn.action_id,
        started_at="2026-08-16T00:00:10Z",
        allowed_tool_ids=("bash",),
    )
    canonical_session_temp = context.action_temp_dir
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE agent_action_steps
            SET runtime_state_checkpoint = ?, runtime_state_checkpoint_version = 4
            WHERE step_id = ?
            """,
            (
                json.dumps(
                    {
                        "manifest_id": context.manifest_id,
                        "execution_session_id": context.execution_session_id,
                    }
                ),
                turn.user_step_id,
            ),
        )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        invocation=_running_invocation(
            invocation_id="invocation-retry",
            tool_request_id="request-retry",
            action_id=turn.action_id,
            step_id=turn.user_step_id,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-08-16T00:00:11Z",
        ),
    )
    temp_file = canonical_session_temp / "retry.tmp"
    temp_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-process-retry",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-retry",
            action_id=turn.action_id,
            resource_kind="process_group",
            status="active",
            created_at="2026-08-16T00:00:11Z",
            pid=123,
            pgid=123,
            process_start_signature="process-start-signature",
            resource_path=None,
        ),
    )
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-retry",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-retry",
            action_id=turn.action_id,
            resource_kind="temp_file",
            status="active",
            created_at="2026-08-16T00:00:12Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(temp_file),
        ),
    )
    original_cleanup = cancel_cleanup.cleanup_runtime_resource
    process_failure_pending = True

    def fail_process_once(resource: StoredToolRuntimeResource) -> None:
        nonlocal process_failure_pending
        if resource.resource_kind == "process_group":
            if process_failure_pending:
                process_failure_pending = False
                raise PermissionError("process interruption denied")
            return
        original_cleanup(resource)

    monkeypatch.setattr(cancel_cleanup, "cleanup_runtime_resource", fail_process_once)

    cleanup_complete = await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason=None,
    )
    assert cleanup_complete is False

    with sqlite3.connect(db_path) as connection:
        first_action_row = connection.execute(
            "SELECT status FROM agent_actions WHERE action_id = ?",
            (turn.action_id,),
        ).fetchone()
        first_session_row = connection.execute(
            "SELECT status FROM execution_sessions WHERE execution_session_id = ?",
            (context.execution_session_id,),
        ).fetchone()
        first_invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = 'invocation-retry'"
        ).fetchone()
        first_resource_rows = connection.execute(
            """
            SELECT resource_kind, status
            FROM tool_runtime_resources
            ORDER BY resource_kind
            """
        ).fetchall()

    assert first_action_row == ("canceled",)
    assert first_session_row == ("canceled",)
    assert first_invocation_row == ("canceled",)
    assert first_resource_rows == [
        ("process_group", "cleanup_failed"),
        ("temp_dir", "active"),
        ("temp_file", "active"),
    ]
    assert temp_file.exists()

    cleanup_complete = await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id=turn.action_id,
        reason=None,
    )
    assert cleanup_complete is True

    with sqlite3.connect(db_path) as connection:
        retry_resource_rows = connection.execute(
            """
            SELECT resource_kind, status
            FROM tool_runtime_resources
            ORDER BY resource_kind
            """
        ).fetchall()
    assert retry_resource_rows == [
        ("process_group", "cleaned"),
        ("temp_dir", "cleaned"),
        ("temp_file", "cleaned"),
    ]
    assert not canonical_session_temp.exists()

    assert await cancel_service.execute_action_cancel(
        user_id="user-1", action_id=turn.action_id, reason=None
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE tool_runtime_resources
            SET status = 'abandoned', cleaned_at = NULL
            WHERE execution_session_id = ? AND resource_kind = 'temp_dir'
              AND tool_invocation_id IS NULL
            """,
            (context.execution_session_id,),
        )
    assert not await cancel_service.execute_action_cancel(
        user_id="user-1", action_id=turn.action_id, reason=None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ActionCancelNotFoundError("missing"), 404),
        (ActionCancelStateConflictError("conflict"), 409),
    ],
)
async def test_cancel_maps_repository_errors_to_http(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_status: int,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    repository = MagicMock()
    repository.cancel_processing_action = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        cancel_service,
        "ActionCancelRepository",
        MagicMock(return_value=repository),
    )
    monkeypatch.setattr(
        cancel_service,
        "read_local_runtime_db_config",
        lambda: (Path("/tmp/runtime.db"), BUSY_TIMEOUT_MS),
    )
    monkeypatch.setattr(
        cancel_service,
        "verify_current_owner",
        lambda _user_id: None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await cancel_service.execute_action_cancel(
            user_id="user-1",
            action_id="action-1",
            reason=None,
        )
    assert exc_info.value.status_code == expected_status


@pytest.mark.asyncio
async def test_terminal_cancel_replay_skips_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    repository = MagicMock()
    repository.cancel_processing_action = AsyncMock(
        return_value=ActionCancelResult(
            changed=False,
            action_status="success",
            suggestion_id=None,
            process_id=None,
            execution_session_ids=(),
        )
    )
    monkeypatch.setattr(
        cancel_service,
        "ActionCancelRepository",
        MagicMock(return_value=repository),
    )
    monkeypatch.setattr(
        cancel_service,
        "read_local_runtime_db_config",
        lambda: (Path("/tmp/runtime.db"), BUSY_TIMEOUT_MS),
    )
    # Canceling as the logged-out owner exercises the real owner check.
    register_logged_out_owner("user-1")
    cleanup = AsyncMock()
    monkeypatch.setattr(cancel_service, "_run_cancel_cleanup_pass", cleanup)

    cleanup_complete = await cancel_service.execute_action_cancel(
        user_id="user-1",
        action_id="action-1",
        reason=None,
    )

    assert cleanup_complete is True
    cleanup.assert_not_awaited()


def test_cleanup_warning_omits_absent_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import action_cancel_service as cancel_service

    append_event = MagicMock()
    monkeypatch.setattr(cancel_service, "append_local_process_event", append_event)
    cancel_service._persist_action_cleanup_warning(
        db_path=Path("/tmp/runtime.db"),
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        process_id="process-1",
        action_id="action-1",
        suggestion_id=None,
        user_id="user-1",
        cleanup_result=ActionCleanupPassResult(
            cleaned_count=0,
            affected_resource_count=1,
            execution_failure_count=1,
            persistence_failure_count=0,
            failed_resource_kinds=("process_group",),
            abandoned_count=1,
        ),
    )
    payload = append_event.call_args.kwargs["payload"]
    assert payload["warning_code"] == "ACTION_CANCEL_CLEANUP_ABANDONED"
    assert "suggestion_id" not in payload

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.invocation_audit import (
    start_local_tool_invocation_audit,
)
from pantaray_agents.local_runtime.tooling.repository import (
    CommandInvocationAuditUpsertInput,
    ToolInvocationSessionConflictError,
    complete_execution_session,
    end_running_command_invocation_audit,
    upsert_command_invocation_audit,
)
from pantaray_agents.local_runtime.tooling.repository.command_invocation_audits import (
    CommandAuditOutcome,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _stub_process_group_resource_registration,
    _stub_subprocess_exec,
)


def _allow_session_process_exec(
    *,
    db_path: Path,
    execution_session_id: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                UPDATE execution_sessions
                SET capability_snapshot_json = ?
                WHERE execution_session_id = ?
                """,
                (
                    json.dumps(
                        {"allowed_capabilities": ["scoped_read", "process_exec_local"]}
                    ),
                    execution_session_id,
                ),
            )


def test_audit_propagates_terminal_session_conflict_without_invocation(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-03-23T00:00:01Z",
    )

    with pytest.raises(ToolInvocationSessionConflictError):
        start_local_tool_invocation_audit(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            step_id="step-terminal-audit",
            tool_id="read",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            args={"path": "notes.txt"},
            tool_request_id="request-terminal-audit",
            started_at="2026-03-23T00:00:02Z",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_invocations"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_successful_bash_writes_command_invocation_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch, stdout=b"hello\n")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-audit-success",
        args={"command": "pwd"},
    )

    assert outcome.status == "success"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                a.execution_kind,
                a.executable_source_kind,
                a.terminal_outcome,
                a.stdout_bytes,
                a.stderr_bytes,
                s.approval_source
            FROM command_invocation_audits AS a
            JOIN approval_sessions AS s
                ON s.approval_session_id = a.approval_session_id
            ORDER BY a.created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert row == (
        "workspace_command",
        "trusted_system_executable",
        "exited",
        len(b"hello\n"),
        0,
        "settings",
    )


@pytest.mark.asyncio
async def test_direct_capability_bash_writes_audit_without_approval_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _allow_session_process_exec(
        db_path=db_path,
        execution_session_id=context.execution_session_id,
    )
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch, stdout=b"hello\n")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-direct-capability-audit",
        args={"command": "pwd"},
    )

    assert outcome.status == "success"
    with sqlite3.connect(db_path) as connection:
        audit_row = connection.execute(
            """
            SELECT approval_session_id, terminal_outcome, stdout_bytes
            FROM command_invocation_audits
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        approval_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM approval_sessions
            WHERE tool_request_id = 'request-direct-capability-audit'
            """
        ).fetchone()

    assert audit_row == (None, "exited", len(b"hello\n"))
    assert approval_count == (0,)


def _write_command_audit(
    db_path: Path,
    *,
    manifest_id: str,
    execution_session_id: str,
    step_id: str,
    outcome: CommandAuditOutcome,
) -> str:
    invocation_id = start_local_tool_invocation_audit(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        step_id=step_id,
        tool_id="bash",
        manifest_id=manifest_id,
        execution_session_id=execution_session_id,
        args={"command": "npm run dev"},
        tool_request_id=f"request-{step_id}",
        started_at="2026-03-23T00:00:00Z",
    )
    upsert_command_invocation_audit(
        db_path=db_path,
        busy_timeout_ms=1_000,
        audit=CommandInvocationAuditUpsertInput(
            invocation_id=invocation_id,
            approval_session_id=None,
            execution_kind="workspace_command",
            executable_source_kind="trusted_system_executable",
            resolved_executable_path="/bin/zsh",
            terminal_outcome=outcome,
            exit_code=None if outcome == "running" else 0,
            signal=None,
            stdout_bytes=10,
            stderr_bytes=0,
            stdout_max_bytes=100,
            stderr_max_bytes=100,
            temp_storage_limit_bytes=100,
            child_count_limit=8,
            open_file_lease_limit=8,
            budget_exceeded_kind=None,
            sandbox_violation_kind=None,
            sandbox_violation_summary=None,
            created_at="2026-03-23T00:00:01Z",
            updated_at="2026-03-23T00:00:01Z",
        ),
    )
    return invocation_id


def _end(db_path: Path, invocation_id: str, *, updated_at: str) -> bool:
    return end_running_command_invocation_audit(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        terminal_outcome="signaled",
        exit_code=None,
        signal=15,
        stdout_bytes=4_096,
        stderr_bytes=12,
        budget_exceeded_kind=None,
        updated_at=updated_at,
    )


def _audit_row(db_path: Path, invocation_id: str) -> tuple[object, ...]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT terminal_outcome, exit_code, signal, stdout_bytes,
                   stderr_bytes, created_at, updated_at
            FROM command_invocation_audits
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()


def test_ending_a_running_audit_applies_once_and_leaves_ended_rows_alone(
    tmp_path: Path,
) -> None:
    # Catches an end that misses its row, overwrites an end already written
    # (a stop racing the process's own exit), or rewrites a one-shot command's
    # audit, and a caller that cannot tell whether its end was recorded.
    db_path, context = _bootstrap_runtime_db(tmp_path)
    ids = {
        "manifest_id": context.manifest_id,
        "execution_session_id": context.execution_session_id,
    }
    running_id = _write_command_audit(
        db_path, **ids, step_id="step-running", outcome="running"
    )
    ended_id = _write_command_audit(
        db_path, **ids, step_id="step-ended", outcome="exited"
    )
    ended_before = _audit_row(db_path, ended_id)

    assert _end(db_path, running_id, updated_at="2026-03-23T00:01:00Z") is True
    assert _end(db_path, running_id, updated_at="2026-03-23T00:02:00Z") is False
    assert _end(db_path, ended_id, updated_at="2026-03-23T00:02:00Z") is False

    assert _audit_row(db_path, running_id) == (
        "signaled",
        None,
        15,
        4_096,
        12,
        "2026-03-23T00:00:01Z",
        "2026-03-23T00:01:00Z",
    )
    assert _audit_row(db_path, ended_id) == ended_before

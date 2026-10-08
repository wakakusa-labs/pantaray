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
    ToolInvocationSessionConflictError,
    complete_execution_session,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
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
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
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

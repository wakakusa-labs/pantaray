from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    execute_broker_tool,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
)


def _patch_args() -> dict[str, object]:
    return {
        "changes": [
            {
                "op": "update",
                "path": "todo.txt",
                "edits": [{"old_lines": ["old line"], "new_lines": ["new line"]}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_execute_broker_tool_applies_workspace_patch(tmp_path: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    needs_read = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-basic-needs-read",
        tool_request_id="request-apply-patch-needs-read",
        args=_patch_args(),
    )
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-basic",
        tool_request_id="request-apply-patch",
        args=_patch_args(),
    )

    assert needs_read.output["status"] == "needs_read"
    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "new line\n"
    assert outcome.output["applied_paths"] == [str(context.workspace_path / "todo.txt")]


@pytest.mark.asyncio
async def test_execute_broker_tool_apply_patch_generates_invocation_for_execution(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    needs_read = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-apply-patch-no-invocation-needs-read",
        args=_patch_args(),
    )
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-apply-patch-no-invocation",
        args=_patch_args(),
    )

    assert needs_read.output["status"] == "needs_read"
    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "new line\n"
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            WHERE invocation_id = 'apply_patch:request-apply-patch-no-invocation'
            """
        ).fetchone()
    assert invocation_row == ("completed",)


@pytest.mark.asyncio
async def test_execute_broker_tool_apply_patch_tracks_workspace_lock_when_invocation_present(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch",
        tool_request_id="request-apply-patch-lock",
        args={
            "changes": [
                {
                    "op": "update",
                    "path": "todo.txt",
                    "edits": [{"old_lines": ["old line"], "new_lines": ["new line"]}],
                }
            ]
        },
    )

    assert outcome.status == "success"
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT status, resource_path
            FROM tool_runtime_resources
            WHERE resource_kind = 'lock'
            ORDER BY created_at ASC
            """
        ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "cleaned"
    assert not Path(str(rows[0][1])).exists()

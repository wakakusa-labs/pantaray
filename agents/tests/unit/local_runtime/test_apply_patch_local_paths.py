from __future__ import annotations

from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["absolute", "dotdot", "relative", "symlink"])
async def test_execute_broker_tool_accepts_authorized_patch_destinations(
    tmp_path: Path,
    spelling: str,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )

    (context.workspace_path / "alias").symlink_to(
        context.workspace_path, target_is_directory=True
    )
    target_path = {
        "absolute": str(context.workspace_path / "todo.md"),
        "dotdot": "new-dir/../todo.md",
        "relative": "todo.md",
        "symlink": "alias/todo.md",
    }[spelling]
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-absolute-path",
        tool_request_id="request-apply-patch-absolute-path",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": target_path,
                    "new_lines": ["created"],
                    "trailing_newline": True,
                }
            ]
        },
    )

    assert outcome.status == "success"
    assert outcome.output["applied_paths"] == [str(context.workspace_path / "todo.md")]
    assert (context.workspace_path / "todo.md").read_text() == "created\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_rejects_multiple_patch_changes(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-apply-patch-multiple-changes",
            tool_request_id="request-apply-patch-multiple-changes",
            args={
                "changes": [
                    {
                        "op": "add",
                        "path": "duplicate.md",
                        "new_lines": ["relative"],
                        "trailing_newline": True,
                    },
                    {
                        "op": "add",
                        "path": "./duplicate.md",
                        "new_lines": ["dot-relative"],
                        "trailing_newline": True,
                    },
                ]
            },
        )

    assert exc_info.value.code == "BROKER_TOOL_ARGS_INVALID"
    assert not (context.workspace_path / "duplicate.md").exists()


@pytest.mark.asyncio
async def test_execute_broker_tool_adds_file_under_new_directory(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-add-new-directory",
        tool_request_id="request-apply-patch-add-new-directory",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": "generated/deep/result.md",
                    "new_lines": ["created"],
                    "trailing_newline": True,
                }
            ]
        },
    )

    assert outcome.status == "success"
    assert (context.workspace_path / "generated/deep/result.md").read_text(
        encoding="utf-8"
    ) == "created\n"

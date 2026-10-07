from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker_structured_patch
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)


def _update_change(
    *,
    path: str,
    old_lines: list[str],
    new_lines: list[str],
    before_lines: list[str] | None = None,
    after_lines: list[str] | None = None,
) -> dict[str, JSONValue]:
    edit: dict[str, JSONValue] = {
        "old_lines": old_lines,
        "new_lines": new_lines,
    }
    if before_lines is not None:
        edit["before_lines"] = before_lines
    if after_lines is not None:
        edit["after_lines"] = after_lines
    return cast(
        dict[str, JSONValue],
        {
            "op": "update",
            "path": path,
            "edits": [edit],
        },
    )


def _bootstrap_typed_runtime_db(tmp_path: Path) -> tuple[Path, ActionExecutionContext]:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    return db_path, cast(ActionExecutionContext, context)


async def _execute_apply_patch_after_needs_read(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    invocation_id: str,
    tool_request_id: str,
    args: dict[str, JSONValue],
):
    needs_read = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id=f"{invocation_id}-needs-read",
        tool_request_id=f"{tool_request_id}-needs-read",
        args=args,
    )
    assert needs_read.status == "success"
    assert needs_read.output["status"] == "needs_read"
    assert needs_read.output["applied_paths"] == []
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
        args=args,
    )


@pytest.mark.asyncio
async def test_apply_patch_update_preserves_existing_file_mode(tmp_path: Path) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "script.sh"
    workspace_file.write_text("echo old\n", encoding="utf-8")
    workspace_file.chmod(0o755)

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-mode",
        tool_request_id="request-apply-patch-mode",
        args={
            "changes": [
                _update_change(
                    path="script.sh",
                    old_lines=["echo old"],
                    new_lines=["echo new"],
                )
            ]
        },
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "echo new\n"
    assert stat.S_IMODE(workspace_file.stat().st_mode) == 0o755


@pytest.mark.asyncio
async def test_apply_patch_derive_failure_makes_no_filesystem_changes(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    first_file = context.workspace_path / "first.txt"
    second_file = context.workspace_path / "second.txt"
    first_file.write_text("old first\n", encoding="utf-8")
    second_file.write_text("current second\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-apply-patch-derive-failure",
            tool_request_id="request-apply-patch-derive-failure",
            args={
                "changes": [
                    _update_change(
                        path="first.txt",
                        old_lines=["old first"],
                        new_lines=["new first"],
                    ),
                    _update_change(
                        path="second.txt",
                        old_lines=["stale second"],
                        new_lines=["new second"],
                    ),
                ]
            },
        )

    assert exc_info.value.code == "BROKER_TOOL_ARGS_INVALID"
    assert first_file.read_text(encoding="utf-8") == "old first\n"
    assert second_file.read_text(encoding="utf-8") == "current second\n"


@pytest.mark.asyncio
async def test_apply_patch_apply_failure_keeps_no_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "first.txt"
    target.write_text("old first\n", encoding="utf-8")
    original_replace = broker_structured_patch.os.replace

    def fail_target_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if dst == target.name and dst_dir_fd is not None:
            raise OSError("simulated replace failure")
        original_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(broker_structured_patch.os, "replace", fail_target_replace)

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-partial",
        tool_request_id="request-apply-patch-partial",
        args={
            "changes": [
                _update_change(
                    path="first.txt",
                    old_lines=["old first"],
                    new_lines=["new first"],
                ),
            ]
        },
    )

    assert outcome.status == "error"
    assert outcome.output["applied_paths"] == []
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PATCH_WRITE_FAILED"
    assert "failed to apply change at path first.txt" in str(error["message"])
    assert "No file changes were kept" in str(error["message"])
    assert "simulated replace failure" in str(error["message"])
    assert target.read_text(encoding="utf-8") == "old first\n"


@pytest.mark.asyncio
async def test_apply_patch_update_uses_before_lines_as_location_hint(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "notes.txt"
    workspace_file.write_text(
        "## First\n- status: old\n\n## Second\n- status: old\n",
        encoding="utf-8",
    )

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-before-hint",
        tool_request_id="request-apply-patch-before-hint",
        args={
            "changes": [
                _update_change(
                    path="notes.txt",
                    before_lines=["## Second"],
                    old_lines=["- status: old"],
                    new_lines=["- status: new"],
                )
            ]
        },
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == (
        "## First\n- status: old\n\n## Second\n- status: new\n"
    )


@pytest.mark.asyncio
async def test_apply_patch_update_uses_after_lines_as_location_hint(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "notes.txt"
    workspace_file.write_text(
        "- status: old\n## First\n\n- status: old\n## Second\n",
        encoding="utf-8",
    )

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-after-hint",
        tool_request_id="request-apply-patch-after-hint",
        args={
            "changes": [
                _update_change(
                    path="notes.txt",
                    old_lines=["- status: old"],
                    new_lines=["- status: new"],
                    after_lines=["## Second"],
                )
            ]
        },
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == (
        "- status: old\n## First\n\n- status: new\n## Second\n"
    )


@pytest.mark.asyncio
async def test_apply_patch_update_allows_omitted_middle_in_old_line(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "config.txt"
    workspace_file.write_text(
        "prompt = prefix abcdefghijklmnopqrstuvwxyz suffix\n",
        encoding="utf-8",
    )

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-omitted-old",
        tool_request_id="request-apply-patch-omitted-old",
        args={
            "changes": [
                _update_change(
                    path="config.txt",
                    old_lines=["prompt = prefix <OMITTED> suffix"],
                    new_lines=["prompt = replacement"],
                )
            ]
        },
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "prompt = replacement\n"


@pytest.mark.asyncio
async def test_apply_patch_update_allows_omitted_middle_in_before_hint(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "config.txt"
    workspace_file.write_text(
        "section: abcdefghijklmnopqrstuvwxyz end\n"
        "value = old\n"
        "\n"
        "section: other end\n"
        "value = old\n",
        encoding="utf-8",
    )

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-omitted-before",
        tool_request_id="request-apply-patch-omitted-before",
        args={
            "changes": [
                _update_change(
                    path="config.txt",
                    before_lines=["section: abc<OMITTED> end"],
                    old_lines=["value = old"],
                    new_lines=["value = new"],
                )
            ]
        },
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == (
        "section: abcdefghijklmnopqrstuvwxyz end\n"
        "value = new\n"
        "\n"
        "section: other end\n"
        "value = old\n"
    )


@pytest.mark.asyncio
async def test_apply_patch_update_treats_omitted_marker_as_literal_new_line(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "config.txt"
    workspace_file.write_text("value = old\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-omitted-new",
        tool_request_id="request-apply-patch-omitted-new",
        args={
            "changes": [
                _update_change(
                    path="config.txt",
                    old_lines=["value = old"],
                    new_lines=["value = <OMITTED>"],
                )
            ]
        },
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "value = <OMITTED>\n"


@pytest.mark.asyncio
async def test_apply_patch_update_rejects_invalid_omitted_pattern(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "config.txt"
    workspace_file.write_text("value = old\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-invalid-omitted",
        tool_request_id="request-apply-patch-invalid-omitted",
        args={
            "changes": [
                _update_change(
                    path="config.txt",
                    old_lines=["<OMITTED> old"],
                    new_lines=["value = new"],
                )
            ]
        },
    )

    assert outcome.status == "error"
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PATCH_LINE_PATTERN_INVALID"
    assert workspace_file.read_text(encoding="utf-8") == "value = old\n"

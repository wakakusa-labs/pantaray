from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.tooling.brokering import (
    broker_structured_patch,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    BrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_patch_read_gate import (
    PatchReadSnapshot,
    change_uses_visible_lines,
    text_sha256,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ApplyPatchDeleteChange,
    ApplyPatchUpdateChange,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_structured_patch import (
    PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ,
    PATCH_ERROR_READ_WINDOW_TOO_LARGE,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    load_action_file_json_result,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.files import (
    workspace_descriptor_access as descriptor_access,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)
from .resource_recovery_test_support import register_running_apply_patch_invocation

TARGET_MISSING_FEEDBACK = (
    "PATCH_TARGET_MISSING: re-read the workspace and target an existing file."
)
TARGET_NOT_FILE_FEEDBACK = (
    "PATCH_TARGET_NOT_FILE: apply_patch can only update or delete files."
)
READ_FAILED_FEEDBACK = (
    "PATCH_READ_FAILED: re-read the target paths after they are readable."
)


def _bootstrap_typed_runtime_db(tmp_path: Path) -> tuple[Path, ActionExecutionContext]:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    return db_path, cast(ActionExecutionContext, context)


def _bootstrap_workspace_write_context(
    tmp_path: Path,
) -> tuple[Path, ActionExecutionContext]:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    return db_path, context


def _update_args(
    *,
    path: str,
    old_lines: list[str],
    new_lines: list[str],
) -> dict[str, JSONValue]:
    return cast(
        dict[str, JSONValue],
        {
            "changes": [
                {
                    "op": "update",
                    "path": path,
                    "edits": [{"old_lines": old_lines, "new_lines": new_lines}],
                }
            ]
        },
    )


async def _execute_initial_update(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    path: str,
    invocation_suffix: str,
) -> BrokerToolOutcome:
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id=f"invocation-{invocation_suffix}",
        tool_request_id=f"request-{invocation_suffix}",
        args=_update_args(path=path, old_lines=["old"], new_lines=["new"]),
    )
    assert isinstance(outcome, BrokerToolOutcome)
    return outcome


def _assert_patch_error(
    outcome: BrokerToolOutcome,
    *,
    code: str,
    llm_feedback: str,
) -> None:
    assert outcome.status == "error"
    assert isinstance(outcome.output, dict)
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == code
    assert error["llm_feedback"] == llm_feedback


@pytest.mark.asyncio
async def test_patch_read_gate_accepts_valid_no_output_completion(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    invocation_id = register_running_apply_patch_invocation(
        db_path=db_path,
        context=context,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE tool_invocations
            SET status = 'completed', completed_at = '2026-03-23T00:00:02Z'
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        )
        connection.execute(
            """
            INSERT INTO tool_outputs(
                output_id,
                invocation_id,
                output_json,
                output_storage_kind,
                redaction_applied,
                created_at
            ) VALUES (?, ?, NULL, NULL, 0, '2026-03-23T00:00:02Z')
            """,
            (invocation_id, invocation_id),
        )

    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-after-no-output",
        tool_request_id="request-after-no-output",
        args=_update_args(path="todo.txt", old_lines=["old"], new_lines=["new"]),
    )

    assert outcome.output["status"] == "needs_read"


@pytest.mark.asyncio
async def test_apply_patch_initial_update_returns_needs_read(tmp_path: Path) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-needs-read",
        tool_request_id="request-apply-patch-needs-read",
        args=_update_args(path="todo.txt", old_lines=["old"], new_lines=["new"]),
    )

    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert outcome.output["applied_paths"] == []
    assert outcome.file_paths == ()
    assert outcome.output["patch_applied"] is False
    assert outcome.output["read_scope"] == "full_file"
    assert outcome.output["text"] == "old\n"
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_missing_target_reports_target_missing(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_workspace_write_context(tmp_path)

    outcome = await _execute_initial_update(
        db_path=db_path,
        context=context,
        path="missing.txt",
        invocation_suffix="apply-patch-missing-target",
    )

    _assert_patch_error(
        outcome,
        code="PATCH_TARGET_MISSING",
        llm_feedback=TARGET_MISSING_FEEDBACK,
    )


@pytest.mark.asyncio
async def test_apply_patch_directory_target_reports_target_not_file(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_workspace_write_context(tmp_path)
    target = context.workspace_path / "target"
    target.mkdir()

    outcome = await _execute_initial_update(
        db_path=db_path,
        context=context,
        path=target.name,
        invocation_suffix="apply-patch-directory-target",
    )

    _assert_patch_error(
        outcome,
        code="PATCH_TARGET_NOT_FILE",
        llm_feedback=TARGET_NOT_FILE_FEEDBACK,
    )


@pytest.mark.asyncio
async def test_apply_patch_symlink_target_reports_target_not_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_workspace_write_context(tmp_path)
    target = context.workspace_path / "target.txt"
    target.write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    original_target = context.workspace_path / "original-target.txt"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == target.name and dir_fd is not None and not swapped:
            swapped = True
            target.rename(original_target)
            target.symlink_to(outside)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(descriptor_access.os, "open", racing_open)

    outcome = await _execute_initial_update(
        db_path=db_path,
        context=context,
        path=target.name,
        invocation_suffix="apply-patch-symlink-target",
    )

    assert swapped is True
    _assert_patch_error(
        outcome,
        code="PATCH_TARGET_NOT_FILE",
        llm_feedback=TARGET_NOT_FILE_FEEDBACK,
    )
    assert "outside secret" not in str(outcome.output)


@pytest.mark.asyncio
async def test_apply_patch_decode_failure_reports_read_failed(tmp_path: Path) -> None:
    db_path, context = _bootstrap_workspace_write_context(tmp_path)
    target = context.workspace_path / "invalid-utf8.txt"
    target.write_bytes(b"\xff")

    outcome = await _execute_initial_update(
        db_path=db_path,
        context=context,
        path=target.name,
        invocation_suffix="apply-patch-decode-failure",
    )

    _assert_patch_error(
        outcome,
        code="PATCH_READ_FAILED",
        llm_feedback=READ_FAILED_FEEDBACK,
    )


@pytest.mark.asyncio
async def test_apply_patch_read_failure_reports_read_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_workspace_write_context(tmp_path)
    target = context.workspace_path / "unreadable.txt"
    target.write_text("old\n", encoding="utf-8")

    def fail_descriptor_read(*_args: object, **_kwargs: object) -> object:
        raise OSError("simulated descriptor read failure")

    monkeypatch.setattr(
        broker_structured_patch,
        "open",
        fail_descriptor_read,
        raising=False,
    )

    outcome = await _execute_initial_update(
        db_path=db_path,
        context=context,
        path=target.name,
        invocation_suffix="apply-patch-read-failure",
    )

    _assert_patch_error(
        outcome,
        code="PATCH_READ_FAILED",
        llm_feedback=READ_FAILED_FEEDBACK,
    )


@pytest.mark.asyncio
async def test_apply_patch_needs_read_stays_on_parent_descriptor_after_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_workspace_write_context(tmp_path)
    parent = context.workspace_path / "parent"
    parent.mkdir()
    target = parent / "document.txt"
    target.write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / target.name).write_text("outside secret\n", encoding="utf-8")
    original_parent = context.workspace_path / "original-parent"
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == target.name and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(original_parent)
            parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(descriptor_access.os, "open", racing_open)

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-needs-read-race",
        tool_request_id="request-apply-patch-needs-read-race",
        args=_update_args(
            path="parent/document.txt",
            old_lines=["missing"],
            new_lines=["new"],
        ),
    )

    assert swapped is True
    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert outcome.output["text"] == "inside\n"
    assert "outside secret" not in str(outcome.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_absolute", [False, True])
async def test_apply_patch_retry_after_needs_read_applies(
    tmp_path: Path, retry_absolute: bool
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")
    args = _update_args(path="todo.txt", old_lines=["old"], new_lines=["new"])

    first = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-retry-needs-read",
        tool_request_id="request-apply-patch-retry-needs-read",
        args=args,
    )
    assert first.output["path"] == str(target)
    if retry_absolute:
        args = _update_args(path=str(target), old_lines=["old"], new_lines=["new"])
    second = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-retry",
        tool_request_id="request-apply-patch-retry",
        args=args,
    )

    assert first.output["status"] == "needs_read"
    assert second.status == "success"
    assert second.output["status"] == "success"
    assert second.output["applied_paths"] == [str(context.workspace_path / "todo.txt")]
    assert target.read_text(encoding="utf-8") == "new\n"


@pytest.mark.asyncio
async def test_apply_patch_retry_reads_large_needs_read_action_file(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "large.txt"
    target.write_text(f"{'x' * 25_000}\nold\n", encoding="utf-8")
    args = _update_args(path="large.txt", old_lines=["old"], new_lines=["new"])

    first = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-needs-read",
        tool_request_id="request-large-needs-read",
        args=args,
    )
    second = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-retry",
        tool_request_id="request-large-retry",
        args=args,
    )

    with sqlite3.connect(db_path) as connection:
        stored_kind = connection.execute(
            """
            SELECT output_storage_kind
            FROM tool_outputs
            WHERE invocation_id = 'invocation-large-needs-read'
            """
        ).fetchone()
        retry_status = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            WHERE invocation_id = 'invocation-large-retry'
            """
        ).fetchone()

    assert first.output["storage"] == "action_file"
    assert stored_kind == ("action_file",)
    assert retry_status == ("completed",)
    loaded_second = load_action_file_json_result(
        action_tool_results_path=context.tool_results_path,
        invocation_id="invocation-large-retry",
        metadata=second.output,
        max_bytes=100_000,
    )
    assert isinstance(loaded_second, dict)
    assert loaded_second["status"] == "success"
    assert target.read_text(encoding="utf-8").endswith("\nnew\n")


@pytest.mark.asyncio
async def test_apply_patch_reads_action_file_from_manifest_root(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    nested_cwd = context.workspace_path / "nested"
    nested_cwd.mkdir()
    target = nested_cwd / "large.txt"
    target.write_text(f"{'x' * 25_000}\nold\n", encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE execution_sessions SET cwd_path = ? WHERE execution_session_id = ?",
            (str(nested_cwd), context.execution_session_id),
        )
    args = _update_args(path="large.txt", old_lines=["old"], new_lines=["new"])
    first = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-manifest-root-read",
        tool_request_id="request-manifest-root-read",
        args=args,
    )
    assert first.output["storage"] == "action_file"
    alias_parent = context.workspace_path / "aliases"
    alias_parent.mkdir()
    cwd_alias = alias_parent / "cwd"
    cwd_alias.symlink_to(nested_cwd, target_is_directory=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE execution_sessions SET cwd_path = ? WHERE execution_session_id = ?",
            (
                str(cwd_alias),
                context.execution_session_id,
            ),
        )

    retry_args = _update_args(path="large.txt", old_lines=["old"], new_lines=["new"])
    second = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-manifest-root-retry",
        tool_request_id="request-manifest-root-retry",
        args=retry_args,
    )

    loaded_second = load_action_file_json_result(
        action_tool_results_path=context.tool_results_path,
        invocation_id="invocation-manifest-root-retry",
        metadata=second.output,
        max_bytes=100_000,
    )
    assert isinstance(loaded_second, dict)
    assert loaded_second["status"] == "success"
    assert target.read_text(encoding="utf-8").endswith("\nnew\n")


@pytest.mark.asyncio
async def test_apply_patch_ignores_prior_result_above_snapshot_read_limit(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    invocation_id = register_running_apply_patch_invocation(
        db_path=db_path,
        context=context,
    )
    stored_result = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ToolResultFinalizationRequest(
            owner=InvocationToolResultOwner(
                invocation_id=invocation_id,
                completed_at="2026-03-23T00:00:02Z",
                status="completed",
                completion_scope="invocation",
            ),
            output={
                "status": "success",
                "applied_paths": ["previous.txt"],
                "diff": "x" * 300_000,
            },
        ),
    )
    assert stored_result.storage_kind == "action_file"

    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-after-large-patch-result",
        tool_request_id="request-after-large-patch-result",
        args=_update_args(path="todo.txt", old_lines=["old"], new_lines=["new"]),
    )

    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_ignores_read_tool_history(tmp_path: Path) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")

    read_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="read",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-read-before-patch",
        tool_request_id="request-read-before-patch",
        args={"path": "todo.txt"},
    )
    patch_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-patch-after-read-tool",
        tool_request_id="request-patch-after-read-tool",
        args=_update_args(path="todo.txt", old_lines=["old"], new_lines=["new"]),
    )

    assert read_outcome.status == "success"
    assert patch_outcome.output["status"] == "needs_read"
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_stale_snapshot_returns_needs_read_again(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")
    args = _update_args(path="todo.txt", old_lines=["old"], new_lines=["new"])

    await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-stale-needs-read",
        tool_request_id="request-stale-needs-read",
        args=args,
    )
    target.write_text("external\n", encoding="utf-8")
    stale_retry = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-stale-retry",
        tool_request_id="request-stale-retry",
        args=args,
    )

    assert stale_retry.status == "success"
    assert stale_retry.output["status"] == "needs_read"
    assert stale_retry.output["text"] == "external\n"
    assert target.read_text(encoding="utf-8") == "external\n"


@pytest.mark.asyncio
async def test_successful_apply_patch_invalidates_prior_snapshot(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    target = context.workspace_path / "todo.txt"
    target.write_text("old\n", encoding="utf-8")
    args = _update_args(path="todo.txt", old_lines=["old"], new_lines=["new"])

    await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-prior-snapshot",
        tool_request_id="request-prior-snapshot",
        args=args,
    )
    add_outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-add-after-snapshot",
        tool_request_id="request-add-after-snapshot",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": "created.txt",
                    "new_lines": ["created"],
                    "trailing_newline": True,
                }
            ]
        },
    )
    retry = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-retry-after-add",
        tool_request_id="request-retry-after-add",
        args=args,
    )

    assert add_outcome.status == "success"
    assert retry.status == "success"
    assert retry.output["status"] == "needs_read"
    assert target.read_text(encoding="utf-8") == "old\n"


@pytest.mark.asyncio
async def test_apply_patch_large_file_returns_target_window(tmp_path: Path) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    lines = [f"- item {index}" for index in range(1, 851)]
    lines[829] = "- target old"
    target = context.workspace_path / "large.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-needs-read",
        tool_request_id="request-large-needs-read",
        args=_update_args(
            path="large.txt",
            old_lines=["- target old"],
            new_lines=["- target new"],
        ),
    )
    retry = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-retry",
        tool_request_id="request-large-retry",
        args=_update_args(
            path="large.txt",
            old_lines=["- target old"],
            new_lines=["- target new"],
        ),
    )

    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert outcome.output["read_scope"] == "target_windows"
    assert outcome.output["file_truncated"] is True
    assert outcome.output["text"] == ""
    windows = outcome.output["windows"]
    assert isinstance(windows, list)
    assert len(windows) == 1
    assert "- target old\n" in str(windows[0]["text"])
    assert "- item 1\n" not in str(windows[0]["text"])
    assert retry.status == "success"
    assert retry.output["status"] == "success"
    assert target.read_text(encoding="utf-8").splitlines()[829] == "- target new"


@pytest.mark.asyncio
async def test_apply_patch_rejects_target_line_larger_than_read_limit(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    oversized_line = "x" * 40_001
    target = context.workspace_path / "large-line.txt"
    target.write_text(f"{oversized_line}\n", encoding="utf-8")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-line",
        tool_request_id="request-large-line",
        args=_update_args(
            path="large-line.txt",
            old_lines=[oversized_line],
            new_lines=["short"],
        ),
    )

    assert outcome.status == "error"
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == PATCH_ERROR_READ_WINDOW_TOO_LARGE
    assert target.read_text(encoding="utf-8") == f"{oversized_line}\n"


@pytest.mark.asyncio
async def test_apply_patch_large_file_delete_requires_full_file_snapshot(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    lines = [f"- item {index}" for index in range(1, 851)]
    target = context.workspace_path / "large-delete.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = {"changes": [{"op": "delete", "path": "large-delete.txt"}]}

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-delete-needs-read",
        tool_request_id="request-large-delete-needs-read",
        args=args,
    )

    assert outcome.status == "error"
    assert outcome.output["status"] == "error"
    assert outcome.output["applied_paths"] == []
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == PATCH_ERROR_DELETE_REQUIRES_FULL_FILE_READ
    assert target.exists()


@pytest.mark.asyncio
async def test_apply_patch_large_file_window_uses_location_hints(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_typed_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    lines = [f"line {index}" for index in range(1, 901)]
    lines[99] = "duplicate"
    lines[699] = "section B"
    lines[700] = "duplicate"
    target = context.workspace_path / "large-hinted.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = {
        "changes": [
            {
                "op": "update",
                "path": "large-hinted.txt",
                "edits": [
                    {
                        "before_lines": ["section B"],
                        "old_lines": ["duplicate"],
                        "new_lines": ["updated duplicate"],
                    }
                ],
            }
        ]
    }

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-hinted-needs-read",
        tool_request_id="request-large-hinted-needs-read",
        args=args,
    )
    retry = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-large-hinted-retry",
        tool_request_id="request-large-hinted-retry",
        args=args,
    )

    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    windows = outcome.output["windows"]
    assert isinstance(windows, list)
    assert len(windows) == 1
    assert "section B\nduplicate\n" in str(windows[0]["text"])
    assert "line 99\nduplicate\nline 101" not in str(windows[0]["text"])
    assert retry.status == "success"
    assert retry.output["status"] == "success"
    updated_lines = target.read_text(encoding="utf-8").splitlines()
    assert updated_lines[99] == "duplicate"
    assert updated_lines[700] == "updated duplicate"


def test_apply_patch_visible_check_rejects_lines_split_across_windows() -> None:
    change = ApplyPatchUpdateChange.model_validate(
        {
            "op": "update",
            "path": "todo.txt",
            "edits": [
                {
                    "before_lines": ["section A"],
                    "old_lines": ["old 1", "old 2"],
                    "new_lines": ["new"],
                    "after_lines": ["section B"],
                }
            ],
        }
    )
    snapshot = PatchReadSnapshot(
        path="todo.txt",
        file_sha256=text_sha256(""),
        full_file=False,
        visible_texts=("section A\nold 1\n", "old 2\nsection B\n"),
    )

    assert not change_uses_visible_lines(change=change, snapshot=snapshot)


def test_apply_patch_visible_check_rejects_partial_delete_snapshot() -> None:
    change = ApplyPatchDeleteChange.model_validate(
        {"op": "delete", "path": "large.txt"}
    )
    snapshot = PatchReadSnapshot(
        path="large.txt",
        file_sha256=text_sha256(""),
        full_file=False,
        visible_texts=("file start\n",),
    )

    assert not change_uses_visible_lines(change=change, snapshot=snapshot)


def test_apply_patch_visible_check_accepts_same_window_sequence() -> None:
    change = ApplyPatchUpdateChange.model_validate(
        {
            "op": "update",
            "path": "todo.txt",
            "edits": [
                {
                    "before_lines": ["section A"],
                    "old_lines": ["old 1", "old 2"],
                    "new_lines": ["new"],
                    "after_lines": ["section B"],
                }
            ],
        }
    )
    snapshot = PatchReadSnapshot(
        path="todo.txt",
        file_sha256=text_sha256(""),
        full_file=False,
        visible_texts=("section A\ncontext\nold 1\nold 2\nmore context\nsection B\n",),
    )

    assert change_uses_visible_lines(change=change, snapshot=snapshot)


def test_apply_patch_visible_check_accepts_later_match_with_hint() -> None:
    change = ApplyPatchUpdateChange.model_validate(
        {
            "op": "update",
            "path": "todo.txt",
            "edits": [
                {
                    "before_lines": ["section B"],
                    "old_lines": ["duplicate"],
                    "new_lines": ["new"],
                }
            ],
        }
    )
    snapshot = PatchReadSnapshot(
        path="todo.txt",
        file_sha256=text_sha256(""),
        full_file=False,
        visible_texts=("section A\nduplicate\nsection B\nduplicate\n",),
    )

    assert change_uses_visible_lines(change=change, snapshot=snapshot)

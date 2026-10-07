from __future__ import annotations

import os
from pathlib import Path

import pytest

from pantaray_agents.local_runtime import descriptor_access
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.models import ActionExecutionContext

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)


def _update_args(
    *,
    path: str,
    old_lines: list[str],
    new_lines: list[str],
) -> dict[str, object]:
    return {
        "changes": [
            {
                "op": "update",
                "path": path,
                "edits": [{"old_lines": old_lines, "new_lines": new_lines}],
            }
        ]
    }


async def _execute_apply_patch_after_needs_read(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    invocation_id: str,
    tool_request_id: str,
    args: dict[str, object],
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
async def test_execute_broker_tool_applies_structured_update(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.md"
    workspace_file.write_text("## Tasks\n- old\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-structured-update",
        tool_request_id="request-apply-patch-structured-update",
        args=_update_args(
            path="todo.md",
            old_lines=["## Tasks", "- old"],
            new_lines=["## Tasks", "- new"],
        ),
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "## Tasks\n- new\n"
    assert outcome.output["applied_paths"] == [str(context.workspace_path / "todo.md")]
    assert "--- todo.md" in str(outcome.output["diff"])


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
        invocation_id="invocation-apply-patch-add",
        tool_request_id="request-apply-patch-add",
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


@pytest.mark.asyncio
async def test_apply_patch_add_rejects_parent_symlink_retarget_during_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    parent = context.workspace_path / "parent"
    parent.mkdir()
    original_parent = context.workspace_path / "original-parent"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    swapped = False

    def retarget_parent_before_descriptor_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == parent.name and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(original_parent)
            parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        descriptor_access.os, "open", retarget_parent_before_descriptor_open
    )

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-add-retarget",
        tool_request_id="request-apply-patch-add-retarget",
        args={
            "changes": [
                {
                    "op": "add",
                    "path": "parent/new.txt",
                    "new_lines": ["new"],
                    "trailing_newline": True,
                }
            ]
        },
    )

    assert swapped is True
    assert outcome.status == "error"
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PATCH_WRITE_FAILED"
    assert not (outside / "new.txt").exists()
    assert not (original_parent / "new.txt").exists()


@pytest.mark.asyncio
async def test_execute_broker_tool_deletes_file(tmp_path: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "obsolete.txt"
    workspace_file.write_text("remove me\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-delete",
        tool_request_id="request-apply-patch-delete",
        args={"changes": [{"op": "delete", "path": "obsolete.txt"}]},
    )

    assert outcome.status == "success"
    assert not workspace_file.exists()
    assert outcome.output["applied_paths"] == [
        str(context.workspace_path / "obsolete.txt")
    ]


@pytest.mark.asyncio
async def test_execute_broker_tool_update_deletes_matched_block(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("keep\nremove\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-delete-block",
        tool_request_id="request-apply-patch-delete-block",
        args=_update_args(
            path="todo.txt",
            old_lines=["remove"],
            new_lines=[],
        ),
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_update_preserves_trailing_newline(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    with_newline = context.workspace_path / "with-newline.txt"
    without_newline = context.workspace_path / "without-newline.txt"
    with_newline.write_text("old\n", encoding="utf-8")
    without_newline.write_text("old", encoding="utf-8")

    await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-trailing-one",
        tool_request_id="request-apply-patch-trailing-one",
        args=_update_args(
            path="with-newline.txt",
            old_lines=["old"],
            new_lines=["new"],
        ),
    )
    await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-trailing-two",
        tool_request_id="request-apply-patch-trailing-two",
        args=_update_args(
            path="without-newline.txt",
            old_lines=["old"],
            new_lines=["new"],
        ),
    )

    assert with_newline.read_text(encoding="utf-8") == "new\n"
    assert without_newline.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_execute_broker_tool_structured_update_treats_prefixes_as_text(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "symbols.txt"
    workspace_file.write_text("+old\n- keep\n  indented\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-prefix-text",
        tool_request_id="request-apply-patch-prefix-text",
        args=_update_args(
            path="symbols.txt",
            old_lines=["+old", "- keep"],
            new_lines=["-new", "+ keep"],
        ),
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "-new\n+ keep\n  indented\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_reports_context_not_found(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("current line\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-not-found",
        tool_request_id="request-apply-patch-not-found",
        args=_update_args(
            path="todo.txt",
            old_lines=["old line"],
            new_lines=["new line"],
        ),
    )

    assert outcome.status == "error"
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PATCH_CONTEXT_NOT_FOUND"
    assert "old_lines copied from the current file" in str(error["llm_feedback"])
    assert "before_lines/after_lines" in str(error["llm_feedback"])
    assert workspace_file.read_text(encoding="utf-8") == "current line\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_reports_ambiguous_context(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.md"
    workspace_file.write_text("same\nx\nsame\n", encoding="utf-8")

    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
        invocation_id="invocation-apply-patch-ambiguous",
        tool_request_id="request-apply-patch-ambiguous",
        args=_update_args(
            path="todo.md",
            old_lines=["same"],
            new_lines=["changed"],
        ),
    )

    assert outcome.status == "error"
    error = outcome.output["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PATCH_CONTEXT_AMBIGUOUS"
    assert workspace_file.read_text(encoding="utf-8") == "same\nx\nsame\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_rejects_multiple_update_edits(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("a\nb\n", encoding="utf-8")

    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-apply-patch-multiple-edits",
            tool_request_id="request-apply-patch-multiple-edits",
            args={
                "changes": [
                    {
                        "op": "update",
                        "path": "todo.txt",
                        "edits": [
                            {"old_lines": ["b"], "new_lines": ["B"]},
                            {"old_lines": ["a"], "new_lines": ["A"]},
                        ],
                    }
                ]
            },
        )
    assert exc_info.value.code == "BROKER_TOOL_ARGS_INVALID"
    assert workspace_file.read_text(encoding="utf-8") == "a\nb\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_rejects_duplicate_raw_paths(
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
            invocation_id="invocation-apply-patch-duplicate-raw",
            tool_request_id="request-apply-patch-duplicate-raw",
            args={
                "changes": [
                    {
                        "op": "add",
                        "path": "duplicate.md",
                        "new_lines": ["first"],
                        "trailing_newline": True,
                    },
                    {
                        "op": "add",
                        "path": "duplicate.md",
                        "new_lines": ["second"],
                        "trailing_newline": True,
                    },
                ]
            },
        )
    assert exc_info.value.code == "BROKER_TOOL_ARGS_INVALID"


@pytest.mark.asyncio
async def test_execute_broker_tool_preflight_does_not_read_or_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    def fail_read_text(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        raise AssertionError(f"preflight must not read file content: {self}")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-apply-patch-preflight",
        preflight_only=True,
        args=_update_args(
            path="todo.txt",
            old_lines=["old line"],
            new_lines=["new line"],
        ),
    )

    assert outcome.status == "success"
    assert outcome.output["applied_paths"] == ["todo.txt"]


@pytest.mark.asyncio
async def test_execute_broker_tool_revalidates_content_after_preflight(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="scoped_write",
    )
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")
    args = _update_args(
        path="todo.txt",
        old_lines=["old line"],
        new_lines=["new line"],
    )

    preflight = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-apply-patch-revalidate-preflight",
        preflight_only=True,
        args=args,
    )
    workspace_file.write_text("changed elsewhere\n", encoding="utf-8")
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-apply-patch-revalidate",
        tool_request_id="request-apply-patch-revalidate",
        args=args,
    )

    assert preflight.status == "success"
    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert outcome.output["applied_paths"] == []
    assert workspace_file.read_text(encoding="utf-8") == "changed elsewhere\n"


@pytest.mark.asyncio
async def test_execute_broker_tool_rejects_patch_string(tmp_path: Path) -> None:
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
            invocation_id="invocation-apply-patch-string",
            tool_request_id="request-apply-patch-string",
            args={"patch": "*** Begin Patch\n*** End Patch"},
        )
    assert exc_info.value.code == "BROKER_TOOL_ARGS_INVALID"

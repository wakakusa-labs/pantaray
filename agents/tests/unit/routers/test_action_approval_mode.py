from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)
from tests.unit.local_runtime.test_tool_broker_approval import _set_prompt_preference

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    execute_broker_tool,
)
from pantaray_agents.routers import action_approval_mode as router_module


def _patch_args(new_line: str = "new line") -> dict[str, object]:
    return {
        "changes": [
            {
                "op": "update",
                "path": "todo.txt",
                "edits": [{"old_lines": ["old line"], "new_lines": [new_line]}],
            }
        ]
    }


def _bind_router_to_db(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> tuple[Path, object]:
    monkeypatch.setattr(
        router_module,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    monkeypatch.setattr(
        router_module,
        "verify_current_owner",
        lambda user_id: None,
    )
    return db_path, None


async def _request_apply_patch(
    *,
    db_path: Path,
    context: object,
    tool_request_id: str,
) -> object:
    """Issue one apply_patch request; preflight runs before the needs_read reply."""

    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        invocation_id=f"invocation-{tool_request_id}",
        tool_request_id=tool_request_id,
        requested_at="2026-03-23T00:00:05Z",
        args=_patch_args(),
    )


async def _apply_patch(
    *,
    db_path: Path,
    context: object,
    tool_request_id: str,
    new_line: str = "new line",
) -> object:
    # apply_patch requires the file to be read first; the first call replies
    # needs_read and the second one is the request under test.
    needs_read = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        invocation_id=f"invocation-{tool_request_id}-read",
        tool_request_id=f"{tool_request_id}-read",
        args=_patch_args(new_line),
    )
    assert needs_read.status == "success"
    assert needs_read.output["status"] == "needs_read"
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,  # type: ignore[attr-defined]
        execution_session_id=context.execution_session_id,  # type: ignore[attr-defined]
        invocation_id=f"invocation-{tool_request_id}",
        tool_request_id=tool_request_id,
        requested_at="2026-03-23T00:00:05Z",
        args=_patch_args(new_line),
    )


@pytest.mark.asyncio
async def test_get_reports_user_default_until_action_override_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _ = _bootstrap_runtime_db(tmp_path)
    _bind_router_to_db(monkeypatch, db_path)

    default_response = await router_module.get_action_approval_mode(
        user_id="user-1", action_id="action-1", resolved_user_id="user-1"
    )
    await router_module.update_action_approval_mode(
        user_id="user-1",
        action_id="action-1",
        body=router_module.ActionApprovalModeUpdateRequest(
            approval_mode="prompt_each_time"
        ),
        resolved_user_id="user-1",
    )
    override_response = await router_module.get_action_approval_mode(
        user_id="user-1", action_id="action-1", resolved_user_id="user-1"
    )

    # With nothing saved, a new Action starts without asking.
    assert (default_response.approval_mode, default_response.source) == (
        "always_allow",
        "user_default",
    )
    assert (override_response.approval_mode, override_response.source) == (
        "prompt_each_time",
        "action",
    )


@pytest.mark.asyncio
async def test_unsaved_default_auto_approves_a_workspace_edit(
    tmp_path: Path,
) -> None:
    # No saved preference, no global grants and no Action override.
    db_path, context = _bootstrap_runtime_db(tmp_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    outcome = await _apply_patch(
        db_path=db_path,
        context=context,
        tool_request_id="request-unsaved-default",
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "new line\n"


@pytest.mark.asyncio
async def test_action_override_auto_approves_without_global_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _bind_router_to_db(monkeypatch, db_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    await router_module.update_action_approval_mode(
        user_id="user-1",
        action_id="action-1",
        body=router_module.ActionApprovalModeUpdateRequest(
            approval_mode="always_allow"
        ),
        resolved_user_id="user-1",
    )
    outcome = await _apply_patch(
        db_path=db_path,
        context=context,
        tool_request_id="request-action-always-allow",
    )

    assert outcome.status == "success"
    assert workspace_file.read_text(encoding="utf-8") == "new line\n"


@pytest.mark.asyncio
async def test_action_override_to_prompt_wins_over_always_allow_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _bind_router_to_db(monkeypatch, db_path)
    _grant_workspace_full_access(db_path=db_path, capability="scoped_write")
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    await router_module.update_action_approval_mode(
        user_id="user-1",
        action_id="action-1",
        body=router_module.ActionApprovalModeUpdateRequest(
            approval_mode="prompt_each_time"
        ),
        resolved_user_id="user-1",
    )

    with pytest.raises(BrokerApprovalRequiredError):
        await _request_apply_patch(
            db_path=db_path,
            context=context,
            tool_request_id="request-action-prompt",
        )
    assert workspace_file.read_text(encoding="utf-8") == "old line\n"


@pytest.mark.asyncio
async def test_pending_approval_is_not_auto_approved_by_a_later_mode_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _bind_router_to_db(monkeypatch, db_path)
    _set_prompt_preference(db_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    with pytest.raises(BrokerApprovalRequiredError):
        await _request_apply_patch(
            db_path=db_path,
            context=context,
            tool_request_id="request-pending",
        )
    await router_module.update_action_approval_mode(
        user_id="user-1",
        action_id="action-1",
        body=router_module.ActionApprovalModeUpdateRequest(
            approval_mode="always_allow"
        ),
        resolved_user_id="user-1",
    )

    # The already pending decision stays explicit: switching the conversation to
    # auto-approval never settles a request the user was already asked about.
    with pytest.raises(BrokerApprovalRequiredError):
        await _request_apply_patch(
            db_path=db_path,
            context=context,
            tool_request_id="request-pending",
        )
    assert workspace_file.read_text(encoding="utf-8") == "old line\n"


@pytest.mark.asyncio
async def test_update_rejects_user_mismatch_and_unknown_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _ = _bootstrap_runtime_db(tmp_path)
    _bind_router_to_db(monkeypatch, db_path)

    with pytest.raises(HTTPException) as forbidden:
        await router_module.update_action_approval_mode(
            user_id="user-1",
            action_id="action-1",
            body=router_module.ActionApprovalModeUpdateRequest(
                approval_mode="always_allow"
            ),
            resolved_user_id="user-2",
        )
    with pytest.raises(HTTPException) as not_found:
        await router_module.update_action_approval_mode(
            user_id="user-1",
            action_id="action-missing",
            body=router_module.ActionApprovalModeUpdateRequest(
                approval_mode="always_allow"
            ),
            resolved_user_id="user-1",
        )

    assert forbidden.value.status_code == 403
    assert not_found.value.status_code == 404

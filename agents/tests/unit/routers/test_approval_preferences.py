from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _seed_broker_actor_process,
)

from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    BrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ToolInvocationStartInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)


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


def _bootstrap_runtime_db(tmp_path: Path) -> tuple[Path, ActionExecutionContext]:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    return db_path, context


def _record_apply_patch_invocation(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    invocation_id: str,
    tool_request_id: str,
    started_at: str,
) -> None:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=tool_request_id,
            user_id="user-1",
            action_id="action-1",
            step_id=f"step-{invocation_id}",
            tool_id="apply_patch",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="surgical_edit",
            network_policy="cloud-proxy-only",
            command_summary_json={"summary_kind": "apply_patch", "target_paths": []},
            capability_snapshot_json={"required_capabilities": ["scoped_write"]},
            request_json={},
            status="running",
            started_at=started_at,
        ),
    )


async def _execute_apply_patch_after_needs_read(
    *,
    db_path: Path,
    context: ActionExecutionContext,
) -> BrokerToolOutcome:
    needs_read = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-after-enable-needs-read",
        tool_request_id="request-after-enable-needs-read",
        args=_patch_args(),
    )
    assert needs_read.status == "success"
    assert needs_read.output["status"] == "needs_read"
    return await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-after-enable",
        tool_request_id="request-after-enable",
        args=_patch_args(),
    )


@pytest.mark.asyncio
async def test_settings_report_always_allow_until_the_user_saves_a_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import (
        approval_preferences as approval_preferences_router,
    )

    db_path, _context = _bootstrap_runtime_db(tmp_path)
    monkeypatch.setattr(
        approval_preferences_router,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )

    async def read_mode() -> str:
        response = await approval_preferences_router.get_workspace_edit_and_command_approval_preference(
            user_id="user-1", resolved_user_id="user-1"
        )
        return response.approval_mode

    unsaved = await read_mode()
    await approval_preferences_router.update_workspace_edit_and_command_approval_preference(
        user_id="user-1",
        body=approval_preferences_router.ApprovalPreferenceUpdateRequest(
            approval_mode="prompt_each_time"
        ),
        resolved_user_id="user-1",
    )

    assert (unsaved, await read_mode()) == ("always_allow", "prompt_each_time")


@pytest.mark.asyncio
async def test_settings_update_to_always_allow_applies_on_next_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import (
        approval_preferences as approval_preferences_router,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")
    monkeypatch.setattr(
        approval_preferences_router,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )

    response = await approval_preferences_router.update_workspace_edit_and_command_approval_preference(
        user_id="user-1",
        body=approval_preferences_router.ApprovalPreferenceUpdateRequest(
            approval_mode="always_allow"
        ),
        resolved_user_id="user-1",
    )
    outcome = await _execute_apply_patch_after_needs_read(
        db_path=db_path,
        context=context,
    )

    assert response.approval_mode == "always_allow"
    assert outcome.status == "success"
    assert outcome.output["status"] == "success"
    assert workspace_file.read_text(encoding="utf-8") == "new line\n"


@pytest.mark.asyncio
async def test_settings_update_to_prompt_each_time_applies_on_next_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.routers import (
        approval_preferences as approval_preferences_router,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")
    monkeypatch.setattr(
        approval_preferences_router,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )

    await approval_preferences_router.update_workspace_edit_and_command_approval_preference(
        user_id="user-1",
        body=approval_preferences_router.ApprovalPreferenceUpdateRequest(
            approval_mode="always_allow"
        ),
        resolved_user_id="user-1",
    )
    response = await approval_preferences_router.update_workspace_edit_and_command_approval_preference(
        user_id="user-1",
        body=approval_preferences_router.ApprovalPreferenceUpdateRequest(
            approval_mode="prompt_each_time"
        ),
        resolved_user_id="user-1",
    )
    _record_apply_patch_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-after-disable",
        tool_request_id="request-after-disable",
        started_at="2026-03-23T00:00:04Z",
    )
    with pytest.raises(BrokerApprovalRequiredError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-after-disable",
            tool_request_id="request-after-disable",
            requested_at="2026-03-23T00:00:05Z",
            args=_patch_args("another line"),
        )

    assert response.approval_mode == "prompt_each_time"

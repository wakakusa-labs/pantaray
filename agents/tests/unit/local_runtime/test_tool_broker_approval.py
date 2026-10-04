from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling import (
    ApprovalPreferenceUpsertInput,
    ApprovalSessionUpsertInput,
    StoredApprovalSession,
    complete_execution_session,
    interrupt_approval_session_for_tool_request,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.brokering import broker_common
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    ApprovalDecisionError,
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerPolicyError,
    apply_approval_decision,
    execute_broker_tool,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
    _record_apply_patch_invocation,
    _record_bash_invocation,
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


def _set_prompt_preference(db_path: Path) -> None:
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-prompt",
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="prompt_each_time",
            applies_to=("workspace_edit_and_command",),
            created_at="2026-03-23T00:00:00Z",
            updated_at="2026-03-23T00:00:00Z",
        ),
    )


@pytest.mark.asyncio
async def test_execute_broker_tool_requires_explicit_approval_for_patch_without_grant(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    with pytest.raises(BrokerApprovalRequiredError, match="requires user approval"):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-approval-patch",
            requested_at="2026-03-23T00:00:02Z",
            args=_patch_args(),
        )

    with sqlite3.connect(db_path) as connection:
        approval_row = connection.execute(
            """
            SELECT status, tool_invocation_id, tool_id
            FROM approval_sessions
            ORDER BY requested_at DESC
            LIMIT 1
            """
        ).fetchone()
        invocation_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM tool_invocations
            WHERE invocation_id = 'apply_patch:request-approval-patch'
            """
        ).fetchone()

    assert approval_row == (
        "pending",
        None,
        "apply_patch",
    )
    assert invocation_count == (0,)


@pytest.mark.asyncio
async def test_approval_replay_does_not_revive_after_session_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    original_upsert = broker_common.upsert_approval_session

    def _terminate_then_upsert(
        *,
        db_path: Path,
        busy_timeout_ms: int,
        approval_session: ApprovalSessionUpsertInput,
    ) -> StoredApprovalSession:
        original_upsert(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            approval_session=approval_session,
        )
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            execution_session_id=context.execution_session_id,
            status="completed",
            completed_at="2026-03-23T00:00:01Z",
        )
        interrupt_approval_session_for_tool_request(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=approval_session.user_id,
            action_id=approval_session.action_id,
            tool_request_id=approval_session.tool_request_id,
            interrupted_at="2026-03-23T00:00:01Z",
        )
        return original_upsert(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            approval_session=approval_session,
        )

    monkeypatch.setattr(
        broker_common,
        "upsert_approval_session",
        _terminate_then_upsert,
    )

    with pytest.raises(BrokerPolicyError, match="approval session creation rejected"):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-terminal-race",
            requested_at="2026-03-23T00:00:02Z",
            args=_patch_args(),
        )

    with sqlite3.connect(db_path) as connection:
        approval_row = connection.execute(
            "SELECT COUNT(*), MIN(status) FROM approval_sessions"
        ).fetchone()
    assert approval_row == (1, "interrupted")


@pytest.mark.asyncio
async def test_execute_broker_tool_runs_after_single_use_approval(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    with pytest.raises(BrokerApprovalRequiredError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-approval-patch",
            tool_request_id="request-approval-patch",
            requested_at="2026-03-23T00:00:02Z",
            args=_patch_args(),
        )
    approval_session_id = exc_info.value.approval_session_id
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id="request-approval-patch",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=approval_session_id,
        decision="approved_once",
        decided_at="2026-03-23T00:00:03Z",
    )
    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-approval-patch",
        tool_request_id="request-approval-patch",
        requested_at="2026-03-23T00:00:04Z",
        args=_patch_args(),
    )

    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert workspace_file.read_text(encoding="utf-8") == "old line\n"
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            WHERE invocation_id = 'invocation-approval-patch'
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT COUNT(*)
            FROM tool_outputs
            WHERE invocation_id = 'invocation-approval-patch'
            """
        ).fetchone()
    assert invocation_row == ("completed",)
    assert output_row == (1,)

    with pytest.raises(BrokerPolicyError, match="already been claimed"):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-approval-patch",
            requested_at="2026-03-23T00:00:05Z",
            args=_patch_args(),
        )


@pytest.mark.asyncio
async def test_execute_broker_tool_allows_reapproval_with_new_request_id_after_denial(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)

    with pytest.raises(BrokerApprovalRequiredError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-bash-denied-1",
            tool_request_id="request-denied-1",
            requested_at="2026-03-23T00:00:02Z",
            args={"command": "pwd"},
        )
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id="request-denied-1",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=exc_info.value.approval_session_id,
        decision="denied",
        decided_at="2026-03-23T00:00:03Z",
    )

    with pytest.raises(BrokerApprovalDeniedError) as denied_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-denied-1",
            requested_at="2026-03-23T00:00:04Z",
            args={"command": "pwd"},
        )
    assert denied_info.value.approval_session_id == exc_info.value.approval_session_id


@pytest.mark.asyncio
async def test_execute_broker_tool_does_not_consume_or_start_on_execution_claim_conflict(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    with pytest.raises(BrokerApprovalRequiredError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-existing-approval",
            tool_request_id="request-claim-conflict",
            requested_at="2026-03-23T00:00:02Z",
            args=_patch_args(),
        )
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id="request-claim-conflict",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=exc_info.value.approval_session_id,
        decision="approved_once",
        decided_at="2026-03-23T00:00:03Z",
    )
    _record_apply_patch_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-existing-approval",
        tool_request_id="request-claim-conflict",
        started_at="2026-03-23T00:00:03Z",
    )

    with pytest.raises(
        BrokerPolicyError,
        match="tool invocation has already been claimed",
    ):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="apply_patch",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-existing-approval",
            tool_request_id="request-claim-conflict",
            requested_at="2026-03-23T00:00:04Z",
            args=_patch_args(),
        )

    with sqlite3.connect(db_path) as connection:
        approval_row = connection.execute(
            """
            SELECT tool_invocation_id, claimed_at
            FROM approval_sessions
            WHERE user_id = 'user-1' AND tool_request_id = 'request-claim-conflict'
            """
        ).fetchone()
        existing_invocation_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM tool_invocations
            WHERE invocation_id = 'invocation-existing-approval'
            """
        ).fetchone()

    assert approval_row == (None, None)
    assert existing_invocation_count == (1,)

    with pytest.raises(BrokerApprovalRequiredError):
        _record_bash_invocation(
            db_path=db_path,
            context=context,
            invocation_id="invocation-bash-denied-2",
            tool_request_id="request-denied-2",
            started_at="2026-03-23T00:00:05Z",
        )
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-bash-denied-2",
            tool_request_id="request-denied-2",
            requested_at="2026-03-23T00:00:05Z",
            args={"command": "pwd"},
        )


@pytest.mark.asyncio
async def test_direct_execution_maps_terminal_session_before_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import direct_execution_start

    db_path, context = _bootstrap_runtime_db(tmp_path)
    (context.workspace_path / "notes.txt").write_text("notes\n", encoding="utf-8")
    original_record = direct_execution_start.record_tool_invocation_start

    def _record_after_terminal(**kwargs):
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            execution_session_id=context.execution_session_id,
            status="completed",
            completed_at="2026-03-23T00:00:01Z",
        )
        original_record(**kwargs)

    monkeypatch.setattr(
        direct_execution_start,
        "record_tool_invocation_start",
        _record_after_terminal,
    )

    with pytest.raises(
        BrokerPolicyError,
        match="execution session no longer accepts tool invocation creation",
    ):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="read",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-terminal-read",
            tool_request_id="request-terminal-read",
            requested_at="2026-03-23T00:00:02Z",
            args={"path": "notes.txt"},
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_invocations"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_apply_approval_decision_rejects_non_pending_session(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)
    _record_bash_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-bash-pending",
        tool_request_id="request-pending",
        started_at="2026-03-23T00:00:01Z",
    )
    with pytest.raises(BrokerApprovalRequiredError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-bash-pending",
            tool_request_id="request-pending",
            requested_at="2026-03-23T00:00:02Z",
            args={"command": "pwd"},
        )
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id="request-pending",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=exc_info.value.approval_session_id,
        decision="denied",
        decided_at="2026-03-23T00:00:03Z",
    )
    with pytest.raises(ApprovalDecisionError, match="is not pending"):
        apply_approval_decision(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_request_id="request-pending",
            user_id="user-1",
            action_id="action-1",
            approval_session_id=exc_info.value.approval_session_id,
            decision="approved_once",
            decided_at="2026-03-23T00:00:04Z",
        )


@pytest.mark.asyncio
async def test_execute_broker_tool_preflight_records_settings_approval_session(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-settings-preflight",
        requested_at="2026-03-23T00:00:02Z",
        preflight_only=True,
        args={"command": "pwd"},
    )

    assert outcome.status == "success"
    with sqlite3.connect(db_path) as connection:
        approval_row = connection.execute(
            """
            SELECT status, approval_source, tool_request_id
            FROM approval_sessions
            WHERE user_id = ? AND tool_request_id = ?
            """,
            ("user-1", "request-settings-preflight"),
        ).fetchone()
        invocation_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM tool_invocations
            WHERE invocation_id = 'bash:request-settings-preflight'
            """
        ).fetchone()

    assert approval_row == (
        "approved_once",
        "settings",
        "request-settings-preflight",
    )
    assert invocation_count == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("approved_login_environment", [False, True])
async def test_bash_approval_is_bound_to_login_environment_choice(
    tmp_path: Path, approved_login_environment: bool
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _set_prompt_preference(db_path)

    async def run_bash(*, use_login_environment: bool, requested_at: str) -> None:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-login-bash",
            tool_request_id="request-login-bash",
            requested_at=requested_at,
            args={
                "command": "gh auth status",
                "use_login_environment": use_login_environment,
                **(
                    {
                        "justification": "Check the pull request with your GitHub account."
                    }
                    if use_login_environment
                    else {}
                ),
            },
        )

    with pytest.raises(BrokerApprovalRequiredError) as exc_info:
        await run_bash(
            use_login_environment=approved_login_environment,
            requested_at="2026-03-23T00:00:02Z",
        )
    apply_approval_decision(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_request_id="request-login-bash",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=exc_info.value.approval_session_id,
        decision="approved_once",
        decided_at="2026-03-23T00:00:03Z",
    )

    with pytest.raises(BrokerPolicyError, match="command summary does not match"):
        await run_bash(
            use_login_environment=not approved_login_environment,
            requested_at="2026-03-23T00:00:04Z",
        )

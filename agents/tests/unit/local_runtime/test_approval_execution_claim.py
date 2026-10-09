from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.approval_identity import (
    build_approval_preference_id,
    build_capability_grant_id,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
    ApprovalPreferenceUpsertInput,
    ApprovalSessionUpsertInput,
    CapabilityGrantCreateInput,
    ToolInvocationStartInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    claim_approval_execution_start,
    complete_execution_session,
    create_capability_grant,
    load_approval_session_by_request,
    record_tool_invocation_start,
    upsert_approval_preference,
    upsert_approval_session,
)

from .broker_test_support import BROKER_ACTOR_PROCESS_ID, _seed_broker_actor_process
from .migrated_db import prepare_test_database
from .resource_recovery_test_support import valid_apply_patch_args


def _insert_agent_action(
    *,
    db_path: Path,
    user_id: str,
    action_id: str,
    created_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id,
                user_id,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, 'processing', ?, ?)
            """,
            (f"suggestion:{action_id}", user_id, created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id,
                user_id,
                suggestion_id,
                initial_user_message_id,
                execution_target_json,
                status,
                final_output,
                prompt_name,
                prompt_version,
                created_at,
                updated_at
            ) VALUES (
                ?, ?, ?, ?, '{"kind":"scratch"}',
                'processing', '', 'test/tooling', 'v1', ?, ?
            )
            """,
            (
                action_id,
                user_id,
                f"suggestion:{action_id}",
                f"message:{action_id}",
                created_at,
                created_at,
            ),
        )


def _bootstrap_claim_runtime(
    tmp_path: Path,
) -> tuple[Path, ActionExecutionContext]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    _insert_agent_action(
        db_path=db_path,
        user_id="user-1",
        action_id="action-1",
        created_at="2026-03-31T00:00:00Z",
    )
    _seed_broker_actor_process(db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-31T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    return db_path, context


def _build_invocation_start(
    *,
    context: ActionExecutionContext,
    invocation_id: str,
    tool_request_id: str,
    started_at: str,
) -> ToolInvocationStartInput:
    return ToolInvocationStartInput(
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
        user_id="user-1",
        action_id="action-1",
        step_id=f"step:{tool_request_id}",
        tool_id="apply_patch",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        cwd=".",
        timeout_ms=5_000,
        intent_class="surgical_edit",
        network_policy=context.network_policy,
        command_summary_json={"summary_kind": "apply_patch", "target_paths": []},
        capability_snapshot_json={"allowed_capabilities": ["scoped_write"]},
        request_json=valid_apply_patch_args(),
        status="running",
        started_at=started_at,
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


def _upsert_prompt_approved_once_session(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    tool_request_id: str,
) -> None:
    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        approval_session=ApprovalSessionUpsertInput(
            approval_session_id=f"approval:{tool_request_id}",
            tool_request_id=tool_request_id,
            user_id="user-1",
            action_id="action-1",
            manifest_id=context.manifest_id,
            expected_execution_session_id=context.execution_session_id,
            tool_invocation_id=None,
            tool_id="apply_patch",
            intent_class="surgical_edit",
            approval_source="prompt",
            status="approved_once",
            approved_capabilities_json={"required_capabilities": ["scoped_write"]},
            command_summary_json={"summary": "patch todo.txt"},
            requested_at="2026-03-31T00:00:01Z",
            decided_at="2026-03-31T00:00:02Z",
            claimed_at=None,
        ),
    )


def _upsert_settings_approved_session(
    *,
    db_path: Path,
    context: ActionExecutionContext,
    tool_request_id: str,
) -> None:
    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        approval_session=ApprovalSessionUpsertInput(
            approval_session_id=f"approval:{tool_request_id}",
            tool_request_id=tool_request_id,
            user_id="user-1",
            action_id="action-1",
            manifest_id=context.manifest_id,
            expected_execution_session_id=context.execution_session_id,
            tool_invocation_id=None,
            tool_id="apply_patch",
            intent_class="surgical_edit",
            approval_source="settings",
            status="approved_once",
            approved_capabilities_json={"required_capabilities": ["scoped_write"]},
            command_summary_json={"summary": "patch todo.txt"},
            requested_at="2026-03-31T00:00:01Z",
            decided_at="2026-03-31T00:00:02Z",
            claimed_at=None,
        ),
    )


def _grant_workspace_scoped_write(
    *,
    db_path: Path,
    manifest_id: str | None = None,
) -> None:
    del manifest_id
    preference_id = build_approval_preference_id(
        user_id="user-1",
        scope_type="global",
        scope_ref=None,
        applies_to=("workspace_edit_and_command",),
    )
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id=preference_id,
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("workspace_edit_and_command",),
            created_at="2026-03-31T00:00:00Z",
            updated_at="2026-03-31T00:00:00Z",
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=1_000,
        grant=CapabilityGrantCreateInput(
            grant_id=build_capability_grant_id(
                user_id="user-1",
                scope_type="global",
                scope_ref=None,
                capability="scoped_write",
                applies_to=("workspace_edit_and_command",),
            ),
            user_id="user-1",
            preference_id=preference_id,
            capability="scoped_write",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at="2026-03-31T00:00:01Z",
        ),
    )


def test_claim_approval_execution_start_consumes_prompt_approval_and_binds_invocation(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _upsert_prompt_approved_once_session(
        db_path=db_path,
        context=context,
        tool_request_id="request-1",
    )

    result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="prompt",
        tool_invocation_id="invocation-1",
        started_at="2026-03-31T00:00:03Z",
        invocation=_build_invocation_start(
            context=context,
            invocation_id="invocation-1",
            tool_request_id="request-1",
            started_at="2026-03-31T00:00:03Z",
        ),
    )

    assert result.status == "updated"
    stored_session = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
    )
    assert stored_session is not None
    assert stored_session.tool_invocation_id == "invocation-1"
    assert stored_session.claimed_at == "2026-03-31T00:00:03Z"


@pytest.mark.parametrize(
    "conflict",
    ["terminal_session", "cross_user", "stale_manifest"],
)
def test_claim_maps_invocation_session_conflict_without_partial_writes(
    tmp_path: Path,
    conflict: str,
) -> None:
    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _upsert_prompt_approved_once_session(
        db_path=db_path, context=context, tool_request_id="request-conflict"
    )
    invocation = _build_invocation_start(
        context=context,
        invocation_id="invocation-conflict",
        tool_request_id="request-conflict",
        started_at="2026-03-31T00:00:03Z",
    )
    if conflict == "terminal_session":
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            execution_session_id=context.execution_session_id,
            status="completed",
            completed_at="2026-03-31T00:00:03Z",
        )
    else:
        field = {
            "cross_user": "user_id",
            "stale_manifest": "manifest_id",
        }[conflict]
        invocation = invocation.model_copy(update={field: f"other-{field}"})

    result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-conflict",
        action_id="action-1",
        approval_session_id="approval:request-conflict",
        approval_source="prompt",
        tool_invocation_id="invocation-conflict",
        started_at="2026-03-31T00:00:04Z",
        invocation=invocation,
    )

    assert result.status == "conflict"
    with sqlite3.connect(db_path) as connection:
        approval = connection.execute(
            "SELECT status, tool_invocation_id, claimed_at FROM approval_sessions "
            "WHERE tool_request_id='request-conflict'"
        ).fetchone()
        invocation_count = connection.execute(
            "SELECT COUNT(*) FROM tool_invocations"
        ).fetchone()
    assert approval == ("approved_once", None, None)
    assert invocation_count == (0,)


def test_claim_binds_an_invocation_row_its_caller_already_recorded(
    tmp_path: Path,
) -> None:
    """Tools that own their audit row (capture_screen) claim the same session."""

    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _upsert_prompt_approved_once_session(
        db_path=db_path,
        context=context,
        tool_request_id="request-1",
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=_build_invocation_start(
            context=context,
            invocation_id="invocation-1",
            tool_request_id="request-1",
            started_at="2026-03-31T00:00:03Z",
        ),
    )

    result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="prompt",
        tool_invocation_id="invocation-1",
        started_at="2026-03-31T00:00:04Z",
        invocation=None,
    )

    assert result.status == "updated"
    stored_session = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
    )
    assert stored_session is not None
    assert stored_session.tool_invocation_id == "invocation-1"
    assert stored_session.claimed_at == "2026-03-31T00:00:04Z"

    replay = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="prompt",
        tool_invocation_id="invocation-2",
        started_at="2026-03-31T00:00:05Z",
        invocation=None,
    )

    assert replay.status == "conflict"


@pytest.mark.parametrize("recorded_tool_request_id", [None, "other-request"])
def test_claim_without_an_invocation_refuses_a_row_it_does_not_own(
    tmp_path: Path,
    recorded_tool_request_id: str | None,
) -> None:
    """An unrecorded or foreign invocation must never receive the claim."""

    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _upsert_prompt_approved_once_session(
        db_path=db_path,
        context=context,
        tool_request_id="request-1",
    )
    if recorded_tool_request_id is not None:
        record_tool_invocation_start(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation=_build_invocation_start(
                context=context,
                invocation_id="invocation-1",
                tool_request_id=recorded_tool_request_id,
                started_at="2026-03-31T00:00:03Z",
            ),
        )

    result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="prompt",
        tool_invocation_id="invocation-1",
        started_at="2026-03-31T00:00:04Z",
        invocation=None,
    )

    assert result.status == "conflict"
    stored_session = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
    )
    assert stored_session is not None
    assert stored_session.claimed_at is None


def test_claim_approval_execution_start_rejects_same_invocation_reentry(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _grant_workspace_scoped_write(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )
    _upsert_settings_approved_session(
        db_path=db_path,
        context=context,
        tool_request_id="request-1",
    )
    first_result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="settings",
        tool_invocation_id="invocation-1",
        started_at="2026-03-31T00:00:03Z",
        invocation=_build_invocation_start(
            context=context,
            invocation_id="invocation-1",
            tool_request_id="request-1",
            started_at="2026-03-31T00:00:03Z",
        ),
    )
    assert first_result.status == "updated"

    second_result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="settings",
        tool_invocation_id="invocation-1",
        started_at="2026-03-31T00:00:04Z",
        invocation=None,
    )

    assert second_result.status == "conflict"


def test_claim_approval_execution_start_rejects_different_invocation_reentry(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _upsert_prompt_approved_once_session(
        db_path=db_path,
        context=context,
        tool_request_id="request-1",
    )
    claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="prompt",
        tool_invocation_id="invocation-1",
        started_at="2026-03-31T00:00:03Z",
        invocation=_build_invocation_start(
            context=context,
            invocation_id="invocation-1",
            tool_request_id="request-1",
            started_at="2026-03-31T00:00:03Z",
        ),
    )

    second_result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        tool_request_id="request-1",
        action_id="action-1",
        approval_session_id="approval:request-1",
        approval_source="prompt",
        tool_invocation_id="invocation-2",
        started_at="2026-03-31T00:00:04Z",
        invocation=_build_invocation_start(
            context=context,
            invocation_id="invocation-2",
            tool_request_id="request-1",
            started_at="2026-03-31T00:00:04Z",
        ),
    )

    assert second_result.status == "conflict"
    with sqlite3.connect(db_path) as connection:
        invocation_count = connection.execute(
            "SELECT COUNT(*) FROM tool_invocations WHERE tool_request_id = ?",
            ("request-1",),
        ).fetchone()
    assert invocation_count == (1,)


@pytest.mark.asyncio
async def test_execute_broker_tool_uses_preflight_settings_approval_at_execution_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import execution_start

    db_path, context = _bootstrap_claim_runtime(tmp_path)
    _grant_workspace_scoped_write(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )
    workspace_file = context.workspace_path / "todo.txt"
    workspace_file.write_text("old line\n", encoding="utf-8")

    preflight = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-settings-race",
        requested_at="2026-03-31T00:00:02Z",
        preflight_only=True,
        args=_patch_args(),
    )
    assert preflight.status == "success"

    original_claim = execution_start.claim_approval_execution_start

    def _revoke_grant_before_claim(**kwargs):
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                UPDATE capability_grants
                SET revoked_at = ?, revocation_reason = ?
                WHERE user_id = ? AND scope_type = 'global' AND scope_ref IS NULL
                    AND revoked_at IS NULL
                """,
                (
                    "2026-03-31T00:00:03Z",
                    "test revoke before claim",
                    "user-1",
                ),
            )
        return original_claim(**kwargs)

    monkeypatch.setattr(
        execution_start,
        "claim_approval_execution_start",
        _revoke_grant_before_claim,
    )

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="apply_patch",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-settings-race",
        requested_at="2026-03-31T00:00:04Z",
        args=_patch_args(),
    )

    assert outcome.status == "success"
    assert outcome.output["status"] == "needs_read"
    assert workspace_file.read_text(encoding="utf-8") == "old line\n"

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from pantaray_agents.local_runtime.agent_state import LocalActionRepository
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.models import (
    CapabilityGrantCreateInput,
    ToolInvocationCompletionInput,
    ToolInvocationFileReferenceInput,
    ToolInvocationStartInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    ToolInvocationSessionConflictError,
    complete_execution_session,
    load_tool_definition,
    record_tool_invocation_start,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.schema.agent.action import StepType

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


def _build_tool_invocation_start(
    *,
    invocation_id: str,
    tool_request_id: str,
    manifest_id: str,
    execution_session_id: str,
    started_at: str,
) -> ToolInvocationStartInput:
    return ToolInvocationStartInput(
        invocation_id=invocation_id,
        tool_request_id=tool_request_id,
        user_id="user-1",
        action_id="action-1",
        step_id=f"step-{invocation_id}",
        tool_id="apply_patch",
        manifest_id=manifest_id,
        execution_session_id=execution_session_id,
        cwd=".",
        timeout_ms=5_000,
        intent_class="surgical_edit",
        network_policy="cloud-proxy-only",
        command_summary_json={"summary_kind": "apply_patch", "target_paths": []},
        capability_snapshot_json={"required_capabilities": ["scoped_write"]},
        request_json=valid_apply_patch_args(),
        status="running",
        started_at=started_at,
    )


def test_tool_invocation_start_rejects_missing_action_without_synthetic_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)

    with pytest.raises(ToolInvocationSessionConflictError):
        record_tool_invocation_start(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation=ToolInvocationStartInput(
                invocation_id="invoke-1",
                tool_request_id="request-1",
                user_id="user-1",
                action_id="action-1",
                step_id="step-1",
                tool_id="thinking",
                manifest_id="manifest-1",
                execution_session_id="session-1",
                cwd=".",
                timeout_ms=None,
                intent_class="read_only",
                network_policy="cloud-proxy-only",
                command_summary_json=None,
                capability_snapshot_json=None,
                request_json={"args": {"thought": "check state"}},
                status="running",
                started_at="2026-03-22T00:00:01Z",
            ),
        )

    with sqlite3.connect(db_path) as connection:
        synthetic_suggestion_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM agent_suggestions
            WHERE suggestion_id LIKE 'synthetic-suggestion:%'
            """
        ).fetchone()[0]
        synthetic_action_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM agent_actions
            WHERE prompt_version = 'synthetic'
            """
        ).fetchone()[0]

    assert synthetic_suggestion_count == 0
    assert synthetic_action_count == 0


def test_tool_invocation_completion_input_rejects_wrong_field_types() -> None:
    with pytest.raises(ValidationError):
        ToolInvocationCompletionInput(
            invocation_id="invoke-1",
            status="completed",
            completed_at="2026-03-22T00:00:02Z",
            output_json={"text": "done"},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied="false",
        )


def test_tool_invocation_completion_input_requires_storage_kind() -> None:
    with pytest.raises(ValidationError):
        ToolInvocationCompletionInput(
            invocation_id="invoke-1",
            status="completed",
            completed_at="2026-03-22T00:00:02Z",
            output_json={"text": "done"},
            output_storage_kind=None,
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        )


def test_tool_invocation_completion_input_accepts_json_null_as_inline_output() -> None:
    completion = ToolInvocationCompletionInput(
        invocation_id="invoke-1",
        status="completed",
        completed_at="2026-03-22T00:00:02Z",
        output_json=None,
        output_storage_kind="inline_json",
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        redaction_applied=False,
    )

    assert completion.output_json is None
    assert completion.output_storage_kind == "inline_json"


def test_capability_grant_create_input_rejects_wrong_field_types() -> None:
    with pytest.raises(ValidationError):
        CapabilityGrantCreateInput(
            grant_id="grant-1",
            user_id="user-1",
            preference_id="pref-1",
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at=123,
        )


def test_tool_runtime_resource_create_input_rejects_wrong_field_types() -> None:
    with pytest.raises(ValidationError):
        ToolRuntimeResourceCreateInput(
            resource_id="resource-1",
            execution_session_id="session-1",
            tool_invocation_id=None,
            action_id="action-1",
            resource_kind="temp_dir",
            status="active",
            created_at="2026-03-22T00:00:02Z",
            pid="123",
            pgid=None,
            process_start_signature=None,
            resource_path="/tmp/resource",
            lock_id=None,
        )


def test_bootstrap_local_tooling_catalog_seeds_core_and_broker_tools(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )

    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT tool_id, is_enabled, intent_class, required_capabilities_json, timeout_ms
            FROM tool_definitions
            WHERE tool_id IN ('thinking', 'read', 'apply_patch', 'bash')
            ORDER BY tool_id
            """
        ).fetchall()

    assert [row[0] for row in rows] == ["apply_patch", "bash", "read", "thinking"]
    assert rows[0][1] == 1
    assert rows[1][1] == 1
    assert rows[2][1] == 1
    assert rows[3][1] == 1
    assert rows[0][2] == "surgical_edit"
    assert rows[1][2] == "process_exec_local"
    assert rows[2][2] == "read_only"
    assert rows[0][3] == '["scoped_write"]'
    assert rows[1][3] == '["process_exec_local"]'
    assert rows[2][4] == 30000


def test_scratch_execution_context_and_tool_audit_are_persisted(tmp_path: Path) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("thinking", "memory_search"),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id="invoke-1",
            tool_request_id="request-1",
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id="thinking",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=None,
            intent_class="read_only",
            network_policy="cloud-proxy-only",
            command_summary_json=None,
            capability_snapshot_json={"allowed_capabilities": []},
            request_json={"args": {"thought": "check state"}},
            status="running",
            started_at="2026-03-22T00:00:01Z",
        ),
    )
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id="invoke-1",
            status="completed",
            completed_at="2026-03-22T00:00:02Z",
            output_json={"text": "done"},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-03-22T00:00:03Z",
    )

    with sqlite3.connect(db_path) as connection:
        manifest_row = connection.execute(
            """
            SELECT action_id, status
            FROM workspace_manifests
            WHERE manifest_id = ?
            """,
            (context.manifest_id,),
        ).fetchone()
        session_row = connection.execute(
            """
            SELECT exec_mode, network_policy, status, capability_snapshot_json
            FROM execution_sessions
            WHERE execution_session_id = ?
            """,
            (context.execution_session_id,),
        ).fetchone()
        invocation_row = connection.execute(
            """
            SELECT tool_id, intent_class, status
            FROM tool_invocations
            WHERE invocation_id = ?
            """,
            ("invoke-1",),
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT invocation_id, output_json, redaction_applied
            FROM tool_outputs
            WHERE invocation_id = ?
            """,
            ("invoke-1",),
        ).fetchone()

    assert manifest_row == ("action-1", "ready")
    assert session_row is not None
    assert session_row[0] == "brokered_file_ops"
    assert session_row[1] == "cloud-proxy-only"
    assert session_row[2] == "completed"
    assert "scoped_read" in str(session_row[3])
    assert "scoped_write" not in str(session_row[3])
    assert "process_exec_local" not in str(session_row[3])
    assert invocation_row == ("thinking", "read_only", "completed")
    assert output_row is not None
    assert output_row[0] == "invoke-1"
    assert '"text": "done"' in str(output_row[1])
    assert output_row[2] == 0

    definition = load_tool_definition(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="thinking",
    )
    assert definition.intent_class == "read_only"
    assert definition.required_capabilities == ()
    assert definition.llm_guide_json["what"]
    assert definition.llm_guide_json["when"]
    assert definition.llm_guide_json["pitfalls"]


def test_tool_invocation_completion_does_not_create_synthetic_action_step(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=_build_tool_invocation_start(
            invocation_id="invoke-1",
            tool_request_id="request-1",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-03-22T00:00:01Z",
        ),
    )

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id="invoke-1",
            status="completed",
            completed_at="2026-03-22T00:00:02Z",
            output_json={"text": "done"},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    with sqlite3.connect(db_path) as connection:
        step_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM agent_action_steps
            WHERE short_step_id LIKE 'synthetic-%-TOOL'
            """
        ).fetchone()[0]
        invocation_row = connection.execute(
            """
            SELECT step_id, status
            FROM tool_invocations
            WHERE invocation_id = 'invoke-1'
            """
        ).fetchone()

    assert step_count == 0
    assert invocation_row == (None, "completed")


def test_tool_invocation_completion_does_not_update_formal_action_step(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id,
                action_id,
                user_id,
                step_number,
                local_step_number,
                short_step_id,
                step_type,
                step_name,
                status,
                started_at,
                completed_at,
                created_at
            ) VALUES (?, ?, ?, 1, 1, ?, 'tool_execution', 'goal_worker::read', 'processing', NULL, NULL, ?)
            """,
            (
                "step-invoke-1",
                "action-1",
                "user-1",
                "G2-5-TOOL",
                "2026-03-22T00:00:02Z",
            ),
        )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=_build_tool_invocation_start(
            invocation_id="invoke-1",
            tool_request_id="request-1",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-03-22T00:00:01Z",
        ),
    )

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id="invoke-1",
            status="completed",
            completed_at="2026-03-22T00:00:02Z",
            output_json={"text": "done"},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    with sqlite3.connect(db_path) as connection:
        step_row = connection.execute(
            """
            SELECT status, started_at, completed_at
            FROM agent_action_steps
            WHERE step_id = 'step-invoke-1'
            """
        ).fetchone()

    assert step_row == ("processing", None, None)


@pytest.mark.asyncio
async def test_saving_formal_action_step_links_matching_tool_invocation(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=_build_tool_invocation_start(
            invocation_id="invoke-1",
            tool_request_id="step-invoke-1",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-03-22T00:00:01Z",
        ),
    )
    repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=1_000)

    await repository.save_action_step(
        step_id="step-invoke-1",
        action_id="action-1",
        step_number=1,
        step_name="goal_worker::read",
        step_type=StepType.TOOL_EXECUTION,
        tool_args={"tool_id": "read", "args": {"path": "notes.txt"}},
        tool_output={"status": "success", "output": {"content": "done"}},
        thinking=None,
        runtime_state_checkpoint=None,
        runtime_state_checkpoint_version=None,
        status="success",
        started_at="2026-03-22T00:00:01Z",
        completed_at="2026-03-22T00:00:02Z",
        retry_count=0,
        goal_handle="G2",
        user_id="user-1",
        short_step_id="G2-5-TOOL",
        local_step_number=5,
        tool_invocation_ids=("invoke-1",),
    )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT step_id
            FROM tool_invocations
            WHERE invocation_id = 'invoke-1'
            """
        ).fetchone()

    assert invocation_row == ("step-invoke-1",)


@pytest.mark.asyncio
async def test_saving_formal_action_step_links_by_invocation_id(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=_build_tool_invocation_start(
            invocation_id="invoke-1",
            tool_request_id="action-1:formal-step-1:1",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-03-22T00:00:01Z",
        ),
    )
    repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=1_000)

    await repository.save_action_step(
        step_id="formal-step-1",
        action_id="action-1",
        step_number=1,
        step_name="tool::apply_patch",
        step_type=StepType.TOOL_EXECUTION,
        tool_args={"tool_id": "apply_patch", "args": valid_apply_patch_args()},
        tool_output={"status": "success", "output": {"status": "success"}},
        status="success",
        started_at="2026-03-22T00:00:01Z",
        completed_at="2026-03-22T00:00:02Z",
        retry_count=0,
        goal_handle="S",
        user_id="user-1",
        short_step_id="S-1-TOOL",
        local_step_number=1,
        tool_invocation_ids=("invoke-1",),
    )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT step_id
            FROM tool_invocations
            WHERE invocation_id = 'invoke-1'
            """
        ).fetchone()

    assert invocation_row == ("formal-step-1",)


@pytest.mark.asyncio
async def test_saving_formal_action_step_links_all_attempt_invocations(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    for invocation_id in ("invoke-1", "invoke-2"):
        record_tool_invocation_start(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation=_build_tool_invocation_start(
                invocation_id=invocation_id,
                tool_request_id=f"request-{invocation_id}",
                manifest_id=context.manifest_id,
                execution_session_id=context.execution_session_id,
                started_at="2026-03-22T00:00:01Z",
            ),
        )
    repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=1_000)

    await repository.save_action_step(
        step_id="formal-step-1",
        action_id="action-1",
        step_number=1,
        step_name="tool::apply_patch",
        step_type=StepType.TOOL_EXECUTION,
        tool_args={"tool_id": "apply_patch", "args": valid_apply_patch_args()},
        tool_output={"status": "success", "output": {"status": "success"}},
        status="success",
        started_at="2026-03-22T00:00:01Z",
        completed_at="2026-03-22T00:00:02Z",
        retry_count=1,
        goal_handle="S",
        user_id="user-1",
        short_step_id="S-1-TOOL",
        local_step_number=1,
        tool_invocation_ids=("invoke-1", "invoke-2"),
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT invocation_id, step_id
            FROM tool_invocations
            WHERE invocation_id IN ('invoke-1', 'invoke-2')
            ORDER BY invocation_id
            """
        ).fetchall()

    assert rows == [("invoke-1", "formal-step-1"), ("invoke-2", "formal-step-1")]


@pytest.mark.asyncio
async def test_saving_formal_action_step_rejects_duplicate_invocation_links(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=_build_tool_invocation_start(
            invocation_id="invoke-1",
            tool_request_id="request-1",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            started_at="2026-03-22T00:00:01Z",
        ),
    )
    repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=1_000)

    with pytest.raises(MigrationError, match="duplicate invocation id"):
        await repository.save_action_step(
            step_id="formal-step-1",
            action_id="action-1",
            step_number=1,
            step_name="tool::apply_patch",
            step_type=StepType.TOOL_EXECUTION,
            tool_args={"tool_id": "apply_patch", "args": valid_apply_patch_args()},
            tool_output={"status": "success", "output": {"status": "success"}},
            status="success",
            started_at="2026-03-22T00:00:01Z",
            completed_at="2026-03-22T00:00:02Z",
            retry_count=0,
            goal_handle="S",
            user_id="user-1",
            short_step_id="S-1-TOOL",
            local_step_number=1,
            tool_invocation_ids=("invoke-1", "invoke-1"),
        )


def test_completion_file_reference_uses_mount_containing_tool_workspace(
    tmp_path: Path,
) -> None:
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
        created_at="2026-03-22T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("apply_patch",),
    )
    other_root = tmp_path / "other-repo"
    other_root.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO workspace_manifest_roots(
                root_id,
                manifest_id,
                source_type,
                source_id,
                display_name,
                canonical_real_path,
                real_path,
                can_read,
                can_apply_patch,
                can_process_read,
                can_process_write,
                created_at
            ) VALUES (
                'root-other',
                ?,
                'folder',
                'folder-other',
                'other-repository',
                ?,
                ?,
                1,
                1,
                1,
                1,
                '2026-03-22T00:00:00Z'
            )
            """,
            (context.manifest_id, str(other_root.resolve()), str(other_root.resolve())),
        )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id="invoke-1",
            tool_request_id="request-1",
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id="apply_patch",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="surgical_edit",
            network_policy="none",
            command_summary_json={
                "summary_kind": "apply_patch",
                "target_paths": ["src/app.py"],
            },
            capability_snapshot_json={"allowed_capabilities": ["scoped_write"]},
            request_json=valid_apply_patch_args(),
            status="running",
            started_at="2026-03-22T00:00:01Z",
        ),
    )

    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id="invoke-1",
            status="completed",
            completed_at="2026-03-22T00:00:02Z",
            output_json={"status": "success", "applied_paths": ["src/app.py"]},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
            file_references=(
                ToolInvocationFileReferenceInput(
                    file_reference_id="file-ref-1",
                    path="src/app.py",
                ),
            ),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT local_path, root_id
            FROM file_references
            WHERE file_reference_id = 'file-ref-1'
            """
        ).fetchone()

    assert row == (
        str(context.workspace_path / "src" / "app.py"),
        "root:action-1:scratch",
    )


def test_load_tool_definition_rejects_missing_required_capabilities_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)

    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                "UPDATE tool_definitions SET required_capabilities_json = NULL WHERE tool_id = ?",
                ("thinking",),
            )

    with pytest.raises(MigrationError):
        load_tool_definition(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="thinking",
        )

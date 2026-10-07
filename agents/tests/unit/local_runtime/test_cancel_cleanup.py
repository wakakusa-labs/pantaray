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
from pantaray_agents.local_runtime.tooling.locks.workspace_lock_coordinator import (
    acquire_workspace_root_lock,
)
from pantaray_agents.local_runtime.tooling.models import (
    ToolInvocationStartInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    cancel_action_runtime_resources,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    create_tool_runtime_resource,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database
from .resource_recovery_test_support import register_running_apply_patch_invocation


def _bootstrap_runtime_db(tmp_path: Path) -> tuple[Path, object]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id="invocation-1",
            tool_request_id="request-1",
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id="bash",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="process_exec_local",
            network_policy="cloud-proxy-only",
            command_summary_json={
                "summary_kind": "bash",
                "command": "run bash",
                "cwd": ".",
                "timeout_ms": 5_000,
            },
            capability_snapshot_json={"required_capabilities": ["process_exec_local"]},
            request_json={"args": {"command": "sleep 30"}},
            status="running",
            started_at="2026-03-23T00:00:01Z",
        ),
    )
    return db_path, context


@pytest.mark.asyncio
async def test_cancel_action_runtime_resources_cleans_temp_file(tmp_path: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    temp_file = context.workspace_path / "cancel-temp.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-1",
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(temp_file),
        ),
    )

    result = await cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.cleaned_count == 1
    assert result.failure_count == 0
    assert result.execution_failure_count == 0
    assert result.persistence_failure_count == 0
    assert result.warning_message is None
    assert not temp_file.exists()
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            "SELECT status FROM tool_runtime_resources WHERE resource_id = 'resource-temp-file'"
        ).fetchone()
        event_row = connection.execute(
            """
            SELECT event_type, message
            FROM tool_runtime_resource_events
            WHERE resource_id = 'resource-temp-file'
            """
        ).fetchone()
    assert resource_row == ("cleaned",)
    assert event_row == (
        "action_cancel_post_cleanup_completed",
        "temp_file: post-terminal action cancel cleanup completed",
    )


@pytest.mark.asyncio
async def test_cancel_action_runtime_resources_cleans_workspace_lock(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_apply_patch_invocation(
        db_path=db_path,
        context=context,
    )
    lease = acquire_workspace_root_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_key=str(context.workspace_path),
        workspace_root=context.workspace_path,
        execution_session_id=context.execution_session_id,
        tool_invocation_id=invocation_id,
        action_id="action-1",
        acquired_at="2026-03-23T00:00:02Z",
    )

    result = await cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.cleaned_count == 1
    assert result.failure_count == 0
    assert result.warning_message is None
    assert not lease.lock_path.exists()
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            "SELECT status FROM tool_runtime_resources WHERE resource_id = ?",
            (lease.resource_id,),
        ).fetchone()
        event_row = connection.execute(
            """
            SELECT event_type, message
            FROM tool_runtime_resource_events
            WHERE resource_id = ?
            """,
            (lease.resource_id,),
        ).fetchone()
    assert resource_row == ("cleaned",)
    assert event_row == (
        "action_cancel_post_cleanup_completed",
        "lock: post-terminal action cancel cleanup completed",
    )


@pytest.mark.asyncio
async def test_cancel_action_runtime_resources_retries_and_records_warning(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    target_dir = context.workspace_path / "not-a-file"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-1",
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(target_dir),
        ),
    )

    result = await cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.cleaned_count == 0
    assert result.failure_count == 1
    assert result.execution_failure_count == 1
    assert result.persistence_failure_count == 0
    assert (
        result.warning_message
        == "failed to clean up 1 post-terminal action resources (kinds: temp_file; execution_failures=1; persistence_failures=0)"
    )
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleanup_attempts
            FROM tool_runtime_resources
            WHERE resource_id = 'resource-temp-file'
            """
        ).fetchone()
        event_row = connection.execute(
            """
            SELECT event_type, message
            FROM tool_runtime_resource_events
            WHERE resource_id = 'resource-temp-file'
            """
        ).fetchone()
    assert resource_row == ("cleanup_failed", 1)
    assert event_row == (
        "action_cancel_post_cleanup_warning",
        "temp_file: post-terminal action cancel cleanup failed: temp_file resource path unexpectedly points to a directory",
    )


@pytest.mark.asyncio
async def test_cancel_action_runtime_resources_counts_one_affected_resource_when_execution_and_persistence_both_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.resources import (
        cancel_cleanup as cancel_cleanup_module,
    )
    from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup_persistence import (
        CleanupPersistenceError,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    target_dir = context.workspace_path / "not-a-file"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-1",
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(target_dir),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM tool_runtime_resources WHERE tool_invocation_id IS NULL"
        )
        connection.commit()

    def _raise_persistence_failure(**_: object) -> object:
        raise CleanupPersistenceError(
            "failed to persist post_terminal cleanup failure",
            resource_status_after_persistence="unchanged",
        )

    monkeypatch.setattr(
        cancel_cleanup_module,
        "persist_action_cleanup_failure",
        _raise_persistence_failure,
    )

    result = await cancel_cleanup_module.cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.affected_resource_count == 1
    assert result.failure_count == 1
    assert result.execution_failure_count == 1
    assert result.persistence_failure_count == 1
    assert (
        result.warning_message
        == "failed to clean up 1 post-terminal action resources (kinds: temp_file; execution_failures=1; persistence_failures=1)"
    )


@pytest.mark.asyncio
async def test_cancel_cleanup_event_failure_rolls_back_abandoned_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    target_dir = context.workspace_path / "abandon-dir"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-1",
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(target_dir),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE tool_runtime_resources
            SET cleanup_attempts = 2
            WHERE resource_id = 'resource-temp-file'
            """
        )
        connection.execute(
            "DELETE FROM tool_runtime_resources WHERE tool_invocation_id IS NULL"
        )
        connection.commit()

    def _fail_event(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("event persistence failed")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_transition_store."
        "_record_tool_runtime_resource_event_in_connection",
        _fail_event,
    )

    result = await cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.affected_resource_count == 1
    assert result.execution_failure_count == 1
    assert result.persistence_failure_count == 1
    assert result.abandoned_count == 0
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleanup_attempts
            FROM tool_runtime_resources
            WHERE resource_id = 'resource-temp-file'
            """
        ).fetchone()
    assert resource_row == ("active", 2)


@pytest.mark.asyncio
async def test_cancel_action_runtime_resources_uses_persistence_failure_message_when_cleanup_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.resources import (
        cancel_cleanup as cancel_cleanup_module,
    )
    from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup_persistence import (
        CleanupPersistenceError,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    temp_file = context.workspace_path / "cancel-temp.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-1",
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(temp_file),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM tool_runtime_resources WHERE tool_invocation_id IS NULL"
        )
        connection.commit()

    def _raise_persistence_failure(**_: object) -> object:
        raise CleanupPersistenceError(
            "failed to persist post_terminal cleanup success for resource resource-temp-file",
            resource_status_after_persistence="cleaned",
        )

    monkeypatch.setattr(
        cancel_cleanup_module,
        "persist_action_cleanup_success",
        _raise_persistence_failure,
    )

    result = await cancel_cleanup_module.cancel_action_runtime_resources(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
        execution_session_id=context.execution_session_id,
    )

    assert result.execution_failure_count == 0
    assert result.persistence_failure_count == 1
    assert result.affected_resource_count == 1
    assert (
        result.warning_message
        == "failed to persist cleanup results for 1 post-terminal action resources (kinds: temp_file; persistence_failures=1)"
    )

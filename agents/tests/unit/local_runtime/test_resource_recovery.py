from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.locks.workspace_lock_coordinator import (
    acquire_workspace_root_lock,
)
from pantaray_agents.local_runtime.tooling.models import (
    ExecutionSessionCreateInput,
    ToolInvocationCompletionInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    complete_execution_session,
    create_execution_session,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources import resource_recovery
from pantaray_agents.local_runtime.tooling.resources.process_identity import (
    read_process_start_signature,
)
from pantaray_agents.local_runtime.tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_periodic_reaper,
    reconcile_tool_runtime_resources_for_startup,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    ToolRuntimeResourceReconciliationError,
    create_tool_runtime_resource,
    finalize_tool_runtime_resources_for_invocation,
    list_tool_runtime_resource_events_for_action,
    list_tool_runtime_resource_events_for_invocation,
    mark_tool_runtime_resource_cleaned,
    mark_tool_runtime_resource_cleanup_failed,
)
from pantaray_agents.local_runtime.tooling.resources.resource_store import (
    list_recoverable_tool_runtime_resources_for_action,
)
from pantaray_agents.local_runtime.tooling.resources.resource_transition_store import (
    ToolRuntimeResourceEventInput,
    persist_tool_runtime_resource_transition,
)

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_apply_patch_invocation,
    register_running_bash_invocation,
)


def test_startup_recovery_cleans_orphan_process_and_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    temp_file = context.workspace_path / "orphan.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    process = subprocess.Popen(["sleep", "30"], preexec_fn=os.setsid)
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_cleanup.process_exists",
        lambda pid: False,
    )
    try:
        create_tool_runtime_resource(
            db_path=db_path,
            busy_timeout_ms=1_000,
            resource=ToolRuntimeResourceCreateInput(
                resource_id="resource-process",
                execution_session_id=context.execution_session_id,
                tool_invocation_id=invocation_id,
                action_id="action-1",
                resource_kind="process_group",
                status="active",
                created_at="2026-03-23T00:00:01Z",
                pid=process.pid,
                pgid=os.getpgid(process.pid),
                process_start_signature=read_process_start_signature(pid=process.pid),
                resource_path=None,
            ),
        )
        create_tool_runtime_resource(
            db_path=db_path,
            busy_timeout_ms=1_000,
            resource=ToolRuntimeResourceCreateInput(
                resource_id="resource-temp-file",
                execution_session_id=context.execution_session_id,
                tool_invocation_id=invocation_id,
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

        recovered_count = reconcile_tool_runtime_resources_for_startup(
            db_path=db_path,
            busy_timeout_ms=1_000,
        )
        assert recovered_count == 2
        assert not temp_file.exists()
        process.wait(timeout=5)
        assert process.returncode is not None

        with sqlite3.connect(db_path) as connection:
            invocation_row = connection.execute(
                """
                SELECT status, completed_at
                FROM tool_invocations
                WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
            output_row = connection.execute(
                """
                SELECT output_json
                FROM tool_outputs
                WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
            resource_rows = connection.execute(
                """
                SELECT resource_kind, status
                FROM tool_runtime_resources
                ORDER BY resource_kind ASC
                """
            ).fetchall()

        assert invocation_row == ("failed", invocation_row[1])
        assert invocation_row[1] is not None
        assert output_row is not None
        assert "StartupRecoveryInterruptedToolInvocation" in str(output_row[0])
        assert resource_rows == [
            ("process_group", "cleaned"),
            ("temp_dir", "active"),
            ("temp_file", "cleaned"),
        ]
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


@pytest.mark.parametrize("trigger", ["startup", "periodic"])
def test_recovery_keeps_path_when_process_cleanup_is_abandoned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    process_invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
        invocation_id="process-invocation",
    )
    path_invocation_id = register_running_bash_invocation(
        db_path=db_path,
        context=context,
        invocation_id="path-invocation",
    )
    temp_file = context.action_temp_dir / "process-owned.patch"
    temp_file.write_text("keep\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="process-owned-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=path_invocation_id,
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
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="uninterruptible-process",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=process_invocation_id,
            action_id="action-1",
            resource_kind="process_group",
            status="active",
            created_at="2026-03-23T00:00:02Z",
            pid=123,
            pgid=123,
            process_start_signature="process-start-signature",
            resource_path=None,
        ),
    )
    if trigger == "periodic":
        for invocation_id in (process_invocation_id, path_invocation_id):
            record_tool_invocation_completion(
                db_path=db_path,
                busy_timeout_ms=1_000,
                completion=ToolInvocationCompletionInput(
                    invocation_id=invocation_id,
                    status="canceled",
                    completed_at="2026-03-23T00:00:03Z",
                    output_json={"canceled": True},
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
            status="canceled",
            completed_at="2026-03-23T00:00:03Z",
        )
    real_cleanup = resource_recovery.cleanup_runtime_resource

    def fail_process_cleanup(resource) -> None:  # noqa: ANN001
        if resource.resource_kind == "process_group":
            raise PermissionError("process interruption denied")
        real_cleanup(resource)

    monkeypatch.setattr(
        resource_recovery,
        "cleanup_runtime_resource",
        fail_process_cleanup,
    )
    reconcile = (
        reconcile_tool_runtime_resources_for_startup
        if trigger == "startup"
        else reconcile_tool_runtime_resources_for_periodic_reaper
    )
    recovered_counts = [
        reconcile(db_path=db_path, busy_timeout_ms=1_000) for _ in range(4)
    ]

    with sqlite3.connect(db_path) as connection:
        resource_rows = connection.execute(
            """
            SELECT resource_kind, tool_invocation_id, status, cleanup_attempts
            FROM tool_runtime_resources
            WHERE execution_session_id = ?
            ORDER BY resource_kind
            """,
            (context.execution_session_id,),
        ).fetchall()
        path_output_row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            (path_invocation_id,),
        ).fetchone()
    assert recovered_counts == [1, 1, 1, 0]
    assert resource_rows == [
        ("process_group", process_invocation_id, "abandoned", 3),
        ("temp_dir", None, "active", 0),
        ("temp_file", path_invocation_id, "active", 0),
    ]
    assert context.action_temp_dir.exists()
    assert temp_file.exists()
    if trigger == "startup":
        assert path_output_row is not None
        blocker_warning = json.loads(str(path_output_row[0]))["warnings"]
        assert len(blocker_warning) == 1
        assert blocker_warning[0]["resource_id"] == "uninterruptible-process"
        assert blocker_warning[0]["resource_kind"] == "process_group"
        assert "process interruption denied" in blocker_warning[0]["message"]


def test_startup_recovery_marks_missing_resources_clean_without_crashing(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="missing-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(context.workspace_path / "missing.patch"),
        ),
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleaned_at, cleanup_error
            FROM tool_runtime_resources
            WHERE resource_id = 'missing-temp-file'
            """
        ).fetchone()
        invocation_row = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()

    assert resource_row == ("cleaned", resource_row[1], None)
    assert resource_row[1] is not None
    assert invocation_row == ("failed",)


def test_startup_recovery_cleans_inflight_workspace_lock_resource(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
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

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    assert not lease.lock_path.exists()
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status
            FROM tool_invocations
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
        resource_row = connection.execute(
            """
            SELECT status
            FROM tool_runtime_resources
            WHERE resource_id = ?
            """,
            (lease.resource_id,),
        ).fetchone()
    event_rows = list_tool_runtime_resource_events_for_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )

    assert invocation_row == ("failed",)
    assert resource_row == ("cleaned",)
    assert [
        (event.event_type, event.message)
        for event in event_rows
        if event.resource_id == lease.resource_id
    ] == [("startup_cleanup_inflight_completed", "lock: startup cleanup completed")]


def test_startup_recovery_retains_legacy_action_session_temp_root(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    legacy_root = context.action_temp_dir.parent
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE execution_sessions SET action_temp_dir = ? "
            "WHERE execution_session_id = ?",
            (str(legacy_root), context.execution_session_id),
        )
        connection.execute(
            "UPDATE tool_runtime_resources SET resource_path = ? "
            "WHERE resource_kind = 'temp_dir' AND tool_invocation_id IS NULL",
            (str(legacy_root),),
        )
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-03-23T00:00:02Z",
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 0
    assert legacy_root.exists()
    assert context.action_temp_dir.exists()
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, tool_invocation_id, action_id
            FROM tool_runtime_resources
            """
        ).fetchone()
    event_rows = list_tool_runtime_resource_events_for_action(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )

    assert resource_row == ("active", None, "action-1")
    assert event_rows == ()


def test_startup_recovery_cleans_descendant_roots_before_parent(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (context.action_temp_dir / "external-link").symlink_to(
        external,
        target_is_directory=True,
    )
    child_session_id = "child-session"
    child_temp = context.action_temp_dir.with_name(child_session_id)
    child_temp.mkdir()
    create_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        session=ExecutionSessionCreateInput(
            execution_session_id=child_session_id,
            user_id="user-1",
            action_id="action-1",
            parent_execution_session_id=context.execution_session_id,
            exec_mode="brokered_file_ops",
            cwd_path=str(context.workspace_path),
            action_temp_dir=str(child_temp),
            app_runtime_python=str(context.app_runtime_python),
            network_policy=context.network_policy,
            read_access_scope=context.read_access_scope,
            capability_snapshot_json={},
            tool_allowlist_json=[],
            status="running",
            started_at="2026-03-23T00:00:01Z",
            expires_at=None,
        ),
    )
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="child-session-root",
            execution_session_id=child_session_id,
            tool_invocation_id=None,
            action_id="action-1",
            resource_kind="temp_dir",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(child_temp),
        ),
    )
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=child_session_id,
        status="canceled",
        completed_at="2026-03-23T00:00:02Z",
    )
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-03-23T00:00:02Z",
    )
    active_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:03Z",
        allowed_tool_ids=("bash",),
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 2
    assert not context.action_temp_dir.exists()
    assert not child_temp.exists()
    assert active_context.action_temp_dir.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    with sqlite3.connect(db_path) as connection:
        root_rows = connection.execute(
            """SELECT execution_session_id, status FROM tool_runtime_resources
            WHERE resource_kind = 'temp_dir' AND tool_invocation_id IS NULL"""
        ).fetchall()
    assert set(root_rows) == {
        (context.execution_session_id, "cleaned"),
        (child_session_id, "cleaned"),
        (active_context.execution_session_id, "active"),
    }


def test_startup_recovery_retains_root_for_running_action_job(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        status="failed",
        completed_at="2026-03-23T00:00:02Z",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, action_id, started_at,
                updated_at, heartbeat_at, current_job_id, next_event_seq
            ) VALUES (
                'process-1', 'user-1', 'action', 'running', 'action-1',
                '2026-03-23T00:00:01Z', '2026-03-23T00:00:01Z',
                '2026-03-23T00:00:01Z', 'job-1', 1
            )
            """
        )
        connection.execute(
            """
            INSERT INTO jobs(
                job_id, user_id, job_type, process_id, status, attempt,
                scheduled_at, started_at, heartbeat_at, logical_key
            ) VALUES (
                'job-1', 'user-1', 'execute_action', 'process-1', 'running', 1,
                '2026-03-23T00:00:01Z', '2026-03-23T00:00:01Z',
                '2026-03-23T00:00:01Z', 'action-1'
            )
            """
        )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 0
    assert context.action_temp_dir.exists()
    with sqlite3.connect(db_path) as connection:
        root_status = connection.execute(
            "SELECT status FROM tool_runtime_resources "
            "WHERE resource_kind = 'temp_dir' AND tool_invocation_id IS NULL"
        ).fetchone()
    assert root_status == ("active",)


def test_resource_transition_rejects_stale_snapshot_after_terminal_transition(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    resource = list_recoverable_tool_runtime_resources_for_action(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )[0]

    cleaned = persist_tool_runtime_resource_transition(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=resource,
        status="cleaned",
        timestamp="2026-03-23T00:00:02Z",
        cleanup_error=None,
        event=ToolRuntimeResourceEventInput(
            event_type="test_cleanup_completed",
            message="cleanup completed",
            tool_invocation_id=None,
        ),
    )
    stale_failure = persist_tool_runtime_resource_transition(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=resource,
        status="cleanup_failed",
        timestamp="2026-03-23T00:00:03Z",
        cleanup_error="stale failure",
        event=ToolRuntimeResourceEventInput(
            event_type="test_cleanup_warning",
            message="cleanup failed",
            tool_invocation_id=None,
        ),
    )

    assert cleaned.applied is True
    assert stale_failure.applied is False
    assert stale_failure.current_resource.status == "cleaned"
    assert stale_failure.current_resource.cleaned_at == "2026-03-23T00:00:02Z"
    assert stale_failure.current_resource.cleanup_attempts == 0
    event_rows = list_tool_runtime_resource_events_for_action(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )
    assert [event.event_type for event in event_rows] == ["test_cleanup_completed"]


def test_legacy_resource_mark_does_not_reverse_terminal_status(tmp_path: Path) -> None:
    db_path, _ = bootstrap_runtime_db(tmp_path)
    resource_id = list_recoverable_tool_runtime_resources_for_action(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )[0].resource_id
    mark_tool_runtime_resource_cleaned(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource_id=resource_id,
        cleaned_at="2026-03-23T00:00:02Z",
    )
    mark_tool_runtime_resource_cleanup_failed(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource_id=resource_id,
        failed_at="2026-03-23T00:00:03Z",
        cleanup_error="stale failure",
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT status, cleaned_at, cleanup_error, cleanup_attempts
            FROM tool_runtime_resources
            WHERE resource_id = ?
            """,
            (resource_id,),
        ).fetchone()
    assert row == ("cleaned", "2026-03-23T00:00:02Z", None, 0)


def test_startup_recovery_cleans_action_scoped_process_group_without_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    process = subprocess.Popen(["sleep", "30"], preexec_fn=os.setsid)
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_cleanup.process_exists",
        lambda pid: False,
    )
    try:
        create_tool_runtime_resource(
            db_path=db_path,
            busy_timeout_ms=1_000,
            resource=ToolRuntimeResourceCreateInput(
                resource_id="sandbox-process-group",
                execution_session_id=context.execution_session_id,
                tool_invocation_id=None,
                action_id="action-1",
                resource_kind="process_group",
                status="active",
                created_at="2026-03-23T00:00:01Z",
                pid=process.pid,
                pgid=os.getpgid(process.pid),
                process_start_signature=read_process_start_signature(pid=process.pid),
                resource_path=None,
            ),
        )

        recovered_count = reconcile_tool_runtime_resources_for_startup(
            db_path=db_path,
            busy_timeout_ms=1_000,
        )

        assert recovered_count == 1
        process.wait(timeout=5)
        with sqlite3.connect(db_path) as connection:
            resource_row = connection.execute(
                """
                SELECT status, tool_invocation_id, action_id
                FROM tool_runtime_resources
                WHERE resource_id = 'sandbox-process-group'
                """
            ).fetchone()
        event_rows = list_tool_runtime_resource_events_for_action(
            db_path=db_path,
            busy_timeout_ms=1_000,
            action_id="action-1",
        )

        assert resource_row == ("cleaned", None, "action-1")
        assert [
            (event.event_type, event.message)
            for event in event_rows
            if event.resource_id == "sandbox-process-group"
        ] == [
            (
                "startup_cleanup_lingering_completed",
                "process_group: startup cleanup completed",
            )
        ]
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def test_startup_recovery_retries_cleanup_failed_resource(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    retry_file = context.workspace_path / "retry.patch"
    retry_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="retry-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(retry_file),
        ),
    )
    mark_tool_runtime_resource_cleanup_failed(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource_id="retry-temp-file",
        failed_at="2026-03-23T00:00:02Z",
        cleanup_error="first cleanup failed",
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    assert not retry_file.exists()
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleanup_attempts
            FROM tool_runtime_resources
            WHERE resource_id = 'retry-temp-file'
            """
        ).fetchone()

    assert resource_row == ("cleaned", 1)


def test_invocation_finalization_cleans_absent_resources(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="finalize-missing-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(context.workspace_path / "already-gone.patch"),
        ),
    )

    finalize_tool_runtime_resources_for_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        finalized_at="2026-03-23T00:00:03Z",
    )

    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleaned_at
            FROM tool_runtime_resources
            WHERE resource_id = 'finalize-missing-temp-file'
            """
        ).fetchone()

    assert resource_row == ("cleaned", "2026-03-23T00:00:03Z")


def test_invocation_resource_finalization_translates_bookkeeping_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="finalize-bookkeeping-failure",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(context.workspace_path / "already-gone.patch"),
        ),
    )

    def _fail_bookkeeping(**_kwargs: object) -> None:
        raise MigrationError("row mismatch")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_recovery_store."
        "persist_tool_runtime_resource_transition",
        _fail_bookkeeping,
    )

    with pytest.raises(ToolRuntimeResourceReconciliationError) as exc_info:
        finalize_tool_runtime_resources_for_invocation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            invocation_id=invocation_id,
            finalized_at="2026-03-23T00:00:03Z",
        )

    assert isinstance(exc_info.value.__cause__, MigrationError)


def test_resource_event_failure_rolls_back_cleaned_status_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="atomic-cleanup",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="temp_file",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(context.workspace_path / "already-absent.patch"),
        ),
    )

    def _fail_event(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("event failed")

    with monkeypatch.context() as context_patch:
        context_patch.setattr(
            "pantaray_agents.local_runtime.tooling.resources.resource_transition_store."
            "_record_tool_runtime_resource_event_in_connection",
            _fail_event,
        )
        with pytest.raises(ToolRuntimeResourceReconciliationError):
            finalize_tool_runtime_resources_for_invocation(
                db_path=db_path,
                busy_timeout_ms=1_000,
                invocation_id=invocation_id,
                finalized_at="2026-03-23T00:00:03Z",
                cleanup_event_type="timeout_cleanup_completed",
                cleanup_message_suffix="timeout cleanup completed",
            )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT status, cleaned_at FROM tool_runtime_resources WHERE resource_id = ?",
            ("atomic-cleanup",),
        ).fetchone()
    assert row == ("active", None)

    finalize_tool_runtime_resources_for_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        finalized_at="2026-03-23T00:00:04Z",
        cleanup_event_type="timeout_cleanup_completed",
        cleanup_message_suffix="timeout cleanup completed",
    )
    events = list_tool_runtime_resource_events_for_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )
    assert [(event.event_type, event.resource_id) for event in events] == [
        ("timeout_cleanup_completed", "atomic-cleanup")
    ]


def test_startup_recovery_cleans_absent_process_without_pgid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="mismatched-process",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="process_group",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=12345,
            pgid=None,
            process_start_signature=None,
            resource_path=None,
        ),
    )
    killpg_calls: list[int] = []
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.process_identity._process_exists",
        lambda _pid: False,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_cleanup.os.killpg",
        lambda pgid, sig: killpg_calls.append(pgid),
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    assert killpg_calls == []
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status
            FROM tool_runtime_resources
            WHERE resource_id = 'mismatched-process'
            """
        ).fetchone()
    assert resource_row == ("cleaned",)


def test_startup_recovery_retries_unsigned_live_process_without_pgid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="unknown-process",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id="action-1",
            resource_kind="process_group",
            status="active",
            created_at="2026-03-23T00:00:01Z",
            pid=12345,
            pgid=None,
            process_start_signature=None,
            resource_path=None,
        ),
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.process_identity._process_exists",
        lambda _pid: True,
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleanup_attempts, cleanup_error
            FROM tool_runtime_resources
            WHERE resource_id = 'unknown-process'
            """
        ).fetchone()
    assert resource_row == (
        "cleanup_failed",
        1,
        "process identity could not be confirmed during cleanup",
    )


def test_startup_recovery_abandons_resource_after_retry_budget(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    target_dir = context.workspace_path / "not-a-file"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="abandon-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
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

    for _ in range(3):
        reconcile_tool_runtime_resources_for_startup(
            db_path=db_path,
            busy_timeout_ms=1_000,
        )

    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, cleanup_attempts
            FROM tool_runtime_resources
            WHERE resource_id = 'abandon-temp-file'
            """
        ).fetchone()

    assert resource_row == ("abandoned", 3)


def test_startup_recovery_fails_inflight_invocation_without_resources(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM tool_runtime_resources")
        connection.commit()
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 0
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status, completed_at
            FROM tool_invocations
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()

    assert invocation_row == ("failed", invocation_row[1])
    assert invocation_row[1] is not None
    assert output_row is not None
    assert "without tracked resources" in str(output_row[0])


def test_startup_recovery_records_lingering_cleanup_events(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    target_dir = context.workspace_path / "event-not-a-file"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="lingering-event-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
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

    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )
    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    event_rows = list_tool_runtime_resource_events_for_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )

    filtered_event_rows = [
        (event.event_type, event.message)
        for event in event_rows
        if event.resource_id == "lingering-event-temp-file"
    ]
    assert filtered_event_rows == [
        (
            "startup_cleanup_inflight_warning",
            "temp_file: startup cleanup failed: temp_file resource path unexpectedly points to a directory",
        ),
        (
            "startup_cleanup_lingering_warning",
            "temp_file: startup cleanup failed: temp_file resource path unexpectedly points to a directory",
        ),
    ]

from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.tooling.locks.workspace_lock_coordinator import (
    acquire_workspace_root_lock,
)
from pantaray_agents.local_runtime.tooling.models import (
    ToolInvocationCompletionInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import complete_execution_session
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_periodic_reaper,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    create_tool_runtime_resource,
    list_tool_runtime_resource_events_for_invocation,
)

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_apply_patch_invocation,
    register_running_bash_invocation,
)


def test_periodic_reaper_leaves_inflight_invocation_owned_by_live_runtime(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)

    recovered_count = reconcile_tool_runtime_resources_for_periodic_reaper(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 0
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
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

    assert invocation_row == ("running",)
    assert output_row is None


def test_periodic_reaper_records_periodic_cleanup_events(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    target_dir = context.workspace_path / "periodic-event-not-a-file"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="periodic-event-temp-file",
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
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id=invocation_id,
            status="completed",
            completed_at="2026-03-23T00:00:02Z",
            output_json={"ok": True},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    reconcile_tool_runtime_resources_for_periodic_reaper(
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
        if event.resource_id == "periodic-event-temp-file"
    ]
    assert filtered_event_rows == [
        (
            "periodic_cleanup_lingering_warning",
            "temp_file: periodic reaper cleanup failed: temp_file resource path unexpectedly points to a directory",
        ),
    ]


def test_periodic_reaper_leaves_inflight_workspace_lock_resource(
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

    recovered_count = reconcile_tool_runtime_resources_for_periodic_reaper(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 0
    assert lease.lock_path.exists()
    event_rows = list_tool_runtime_resource_events_for_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )

    assert [
        (event.event_type, event.message)
        for event in event_rows
        if event.resource_id == lease.resource_id
    ] == []


def test_periodic_reaper_waits_for_action_scoped_session_to_be_terminal(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    temp_file = context.workspace_path / "action-scoped-temp.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="action-scoped-temp-file",
            execution_session_id=context.execution_session_id,
            tool_invocation_id=None,
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

    live_recovered_count = reconcile_tool_runtime_resources_for_periodic_reaper(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert live_recovered_count == 0
    assert temp_file.exists()

    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        status="completed",
        completed_at="2026-03-23T00:00:02Z",
    )
    terminal_recovered_count = reconcile_tool_runtime_resources_for_periodic_reaper(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert terminal_recovered_count == 2
    assert not temp_file.exists()
    assert not context.action_temp_dir.exists()


def test_periodic_reaper_does_not_overwrite_terminal_output_for_lingering_resource(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    completion = ToolInvocationCompletionInput(
        invocation_id=invocation_id,
        status="completed",
        completed_at="2026-03-23T00:00:02Z",
        output_json={"ok": True},
        output_storage_kind="inline_json",
        search_text=None,
        stdout_text=None,
        stderr_text=None,
        redaction_applied=False,
    )
    temp_file = context.workspace_path / "lingering-temp.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="periodic-lingering-temp-file",
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
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=completion,
    )

    recovered_count = reconcile_tool_runtime_resources_for_periodic_reaper(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status
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
            ORDER BY created_at ASC
            """,
            (invocation_id,),
        ).fetchall()
        resource_row = connection.execute(
            """
            SELECT status
            FROM tool_runtime_resources
            WHERE resource_id = 'periodic-lingering-temp-file'
            """
        ).fetchone()

    assert invocation_row == ("completed",)
    assert output_row == [('{"ok": true}',)]
    assert resource_row == ("cleaned",)

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime import (
    list_action_recovery_audit_events,
    list_invocation_recovery_audit_events,
    list_runtime_recovery_audit_events,
)
from pantaray_agents.local_runtime.runtime.runtime_lock_repository import (
    create_runtime_lock_resource,
    record_runtime_lock_event,
)
from pantaray_agents.local_runtime.tooling.models import (
    ToolInvocationCompletionInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.cancel_cleanup import (
    cancel_action_runtime_resources,
)
from pantaray_agents.local_runtime.tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_periodic_reaper,
    reconcile_tool_runtime_resources_for_startup,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    create_tool_runtime_resource,
)
from pantaray_agents.local_runtime.tooling.resources.resource_tracking import (
    register_path_resource,
)
from pantaray_agents.local_runtime.tooling.resources.tool_invocation_recovery import (
    cancel_inflight_tool_invocation,
)

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_bash_invocation,
)


def test_list_action_recovery_audit_events_normalizes_resource_and_invocation_events(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    temp_file = context.workspace_path / "startup-audit.patch"
    temp_file.write_text("temporary\n", encoding="utf-8")
    register_path_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        tool_invocation_id=invocation_id,
        action_id="action-1",
        resource_kind="temp_file",
        resource_path=temp_file,
        created_at="2026-03-23T00:00:01Z",
    )

    recovered_count = reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 1
    events = list_action_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )

    assert len(events) == 2
    assert events[0].scope == "tool_resource"
    assert events[0].invocation_id == invocation_id
    assert events[0].event_type == "startup_cleanup_inflight_completed"
    assert events[0].severity == "info"
    assert events[1].scope == "tool_invocation"
    assert events[1].invocation_id == invocation_id
    assert events[1].event_type == "startup_tool_invocation_warning"
    assert events[1].severity == "warning"


def test_list_invocation_recovery_audit_events_projects_cancel_output(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)

    result = cancel_inflight_tool_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:02Z",
    )

    assert result.canceled is True
    events = list_invocation_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )

    assert len(events) == 1
    assert events[0].scope == "tool_invocation"
    assert events[0].event_type == "action_cancel_tool_invocation_warning"
    assert events[0].severity == "warning"


def test_recovery_audit_ignores_valid_no_output_completion(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
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

    events = list_invocation_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )

    assert all(event.scope != "tool_invocation" for event in events)


def test_recovery_audit_ignores_inline_json_null_completion(tmp_path: Path) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id=invocation_id,
            status="completed",
            completed_at="2026-03-23T00:00:02Z",
            output_json=None,
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    events = list_invocation_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
    )

    assert all(event.scope != "tool_invocation" for event in events)


def test_list_action_recovery_audit_events_projects_cancel_cleanup_failure(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = register_running_bash_invocation(db_path=db_path, context=context)
    target_dir = context.workspace_path / "cancel-cleanup-dir"
    target_dir.mkdir()
    create_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="cancel-temp-file",
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
    result = cancel_inflight_tool_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:02Z",
    )
    assert result.canceled is True

    cancel_result = asyncio.run(
        cancel_action_runtime_resources(
            db_path=db_path,
            busy_timeout_ms=1_000,
            action_id="action-1",
            execution_session_id=context.execution_session_id,
            run_process_id=None,
        )
    )

    assert cancel_result.failure_count == 1
    events = list_action_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )
    filtered_events = [
        event
        for event in events
        if event.scope == "tool_invocation" or event.resource_id == "cancel-temp-file"
    ]

    assert [event.event_type for event in filtered_events] == [
        "action_cancel_tool_invocation_warning",
        "action_cancel_post_cleanup_warning",
    ]
    assert [event.severity for event in filtered_events] == ["warning", "warning"]


def test_list_action_recovery_audit_events_projects_periodic_cleanup_for_terminal_resource(
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
    temp_file = context.workspace_path / "periodic-lingering.patch"
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
    events = list_action_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        action_id="action-1",
    )
    filtered_events = [
        event for event in events if event.resource_id == "periodic-lingering-temp-file"
    ]

    assert [event.event_type for event in filtered_events] == [
        "periodic_cleanup_lingering_completed"
    ]
    assert [event.severity for event in filtered_events] == ["info"]


def test_list_runtime_recovery_audit_events_normalizes_event_severity(
    tmp_path: Path,
) -> None:
    db_path, _context = bootstrap_runtime_db(tmp_path)
    resource_id = create_runtime_lock_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_path=tmp_path / "runtime.db.runtime.lock",
        lock_id="runtime-lock-id",
        owner_pid=1234,
        created_at="2026-03-23T00:00:00Z",
    )
    record_runtime_lock_event(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource_id=resource_id,
        event_type="periodic_runtime_lock_warning",
        message="periodic runtime global lock cleanup failed: stale file remained",
        created_at="2026-03-23T00:00:01Z",
    )
    record_runtime_lock_event(
        db_path=db_path,
        busy_timeout_ms=1_000,
        resource_id=resource_id,
        event_type="periodic_runtime_lock_abandoned",
        message="periodic runtime global lock cleanup abandoned",
        created_at="2026-03-23T00:00:02Z",
    )

    events = list_runtime_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert [event.scope for event in events] == ["runtime_lock", "runtime_lock"]
    assert [event.event_type for event in events] == [
        "periodic_runtime_lock_warning",
        "periodic_runtime_lock_abandoned",
    ]
    assert [event.severity for event in events] == ["warning", "abandoned"]

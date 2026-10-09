from __future__ import annotations

import multiprocessing
import sqlite3
from multiprocessing.synchronize import Event as MultiprocessingEvent
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.locks import (
    workspace_lock as workspace_lock_module,
)
from pantaray_agents.local_runtime.tooling.locks.workspace_lock import (
    WorkspaceLockFile,
    WorkspaceLockRecord,
    create_workspace_lock_file,
    read_workspace_lock_record,
    remove_workspace_lock_file_if_owned,
    workspace_root_lock_path,
)
from pantaray_agents.local_runtime.tooling.locks.workspace_lock_coordinator import (
    WorkspaceLockConflictError,
    acquire_workspace_root_lock,
    release_workspace_lock,
)
from pantaray_agents.local_runtime.tooling.models import (
    StoredToolRuntimeResource,
    ToolInvocationCompletionInput,
    ToolInvocationStartInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.resource_cleanup import (
    cleanup_runtime_resource,
)

from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    valid_apply_patch_request_json,
)

PROCESS_SYNC_TIMEOUT_SECONDS = 30


def _register_invocation(
    *,
    db_path: Path,
    context: object,
    invocation_id: str,
    status: str = "running",
) -> None:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=f"request-{invocation_id}",
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
            request_json=valid_apply_patch_request_json(),
            status=status,
            started_at="2026-03-23T00:00:01Z",
        ),
    )


def _remove_workspace_lock_after_read(
    lock_path: Path,
    read_completed: MultiprocessingEvent,
    continue_removal: MultiprocessingEvent,
) -> None:
    original_read = workspace_lock_module.read_workspace_lock_record

    def paused_read(*, lock_path: Path) -> WorkspaceLockRecord | None:
        record = original_read(lock_path=lock_path)
        read_completed.set()
        if not continue_removal.wait(timeout=PROCESS_SYNC_TIMEOUT_SECONDS):
            raise TimeoutError("timed out waiting to continue workspace lock removal")
        return record

    workspace_lock_module.read_workspace_lock_record = paused_read
    remove_workspace_lock_file_if_owned(
        lock_path=lock_path,
        tool_invocation_id="old-invocation",
        lock_id="old-lock-id",
    )


def _create_replacement_workspace_lock(
    lock_path: Path,
    started: MultiprocessingEvent,
    completed: MultiprocessingEvent,
) -> None:
    started.set()
    create_workspace_lock_file(
        lock_file=WorkspaceLockFile(
            lock_path=lock_path,
            record=WorkspaceLockRecord(
                lock_id="new-lock-id",
                tool_invocation_id="new-invocation",
                execution_session_id="session-1",
                lock_key="workspace-1",
                locked_path="/workspace-1",
                acquired_at="2026-03-23T00:00:02Z",
            ),
        )
    )
    completed.set()


def test_acquire_and_release_workspace_lock_registers_cleaned_resource(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-1",
    )

    lease = acquire_workspace_root_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_key=str(context.workspace_path),
        workspace_root=context.workspace_path,
        execution_session_id=context.execution_session_id,
        tool_invocation_id="invocation-lock-1",
        action_id="action-1",
        acquired_at="2026-03-23T00:00:02Z",
    )

    assert lease.lock_path.exists()
    release_result = release_workspace_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lease=lease,
        released_at="2026-03-23T00:00:03Z",
    )

    assert release_result.warning_message is None
    assert not lease.lock_path.exists()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT status
            FROM tool_runtime_resources
            WHERE resource_id = ?
            """,
            (lease.resource_id,),
        ).fetchone()

    assert row == ("cleaned",)


def test_workspace_lock_conflict_fails_closed_for_running_owner(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-1",
    )
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-2",
    )
    lease = acquire_workspace_root_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_key=str(context.workspace_path),
        workspace_root=context.workspace_path,
        execution_session_id=context.execution_session_id,
        tool_invocation_id="invocation-lock-1",
        action_id="action-1",
        acquired_at="2026-03-23T00:00:02Z",
    )

    try:
        with pytest.raises(WorkspaceLockConflictError):
            acquire_workspace_root_lock(
                db_path=db_path,
                busy_timeout_ms=1_000,
                lock_key=str(context.workspace_path),
                workspace_root=context.workspace_path,
                execution_session_id=context.execution_session_id,
                tool_invocation_id="invocation-lock-2",
                action_id="action-1",
                acquired_at="2026-03-23T00:00:03Z",
            )
    finally:
        release_workspace_lock(
            db_path=db_path,
            busy_timeout_ms=1_000,
            lease=lease,
            released_at="2026-03-23T00:00:04Z",
        )


def test_workspace_lock_acquisition_cleans_stale_terminal_owner(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-1",
    )
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-2",
    )
    acquire_workspace_root_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_key=str(context.workspace_path),
        workspace_root=context.workspace_path,
        execution_session_id=context.execution_session_id,
        tool_invocation_id="invocation-lock-1",
        action_id="action-1",
        acquired_at="2026-03-23T00:00:02Z",
    )
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id="invocation-lock-1",
            status="completed",
            completed_at="2026-03-23T00:00:03Z",
            output_json={"ok": True},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    second_lease = acquire_workspace_root_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_key=str(context.workspace_path),
        workspace_root=context.workspace_path,
        execution_session_id=context.execution_session_id,
        tool_invocation_id="invocation-lock-2",
        action_id="action-1",
        acquired_at="2026-03-23T00:00:04Z",
    )

    assert second_lease.lock_path.exists()
    with sqlite3.connect(db_path) as connection:
        resource_rows = connection.execute(
            """
            SELECT tool_invocation_id, status
            FROM tool_runtime_resources
            WHERE resource_kind = 'lock'
            ORDER BY created_at ASC
            """
        ).fetchall()
        event_row = connection.execute(
            """
            SELECT event_type
            FROM tool_runtime_resource_events
            WHERE tool_invocation_id = 'invocation-lock-1'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert resource_rows == [
        ("invocation-lock-1", "cleaned"),
        ("invocation-lock-2", "active"),
    ]
    assert event_row == ("workspace_lock_stale_completed",)

    release_workspace_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lease=second_lease,
        released_at="2026-03-23T00:00:05Z",
    )


def test_workspace_lock_acquisition_fails_closed_for_unknown_lock_file(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-1",
    )
    lock_path = workspace_root_lock_path(
        db_path=db_path,
        lock_key=str(context.workspace_path),
    )
    create_workspace_lock_file(
        lock_file=WorkspaceLockFile(
            lock_path=lock_path,
            record=WorkspaceLockRecord(
                lock_id="unknown-lock-id",
                tool_invocation_id="unknown-invocation",
                execution_session_id=context.execution_session_id,
                lock_key=str(context.workspace_path),
                locked_path=str(context.workspace_path),
                acquired_at="2026-03-23T00:00:02Z",
            ),
        )
    )

    with pytest.raises(WorkspaceLockConflictError):
        acquire_workspace_root_lock(
            db_path=db_path,
            busy_timeout_ms=1_000,
            lock_key=str(context.workspace_path),
            workspace_root=context.workspace_path,
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-lock-1",
            action_id="action-1",
            acquired_at="2026-03-23T00:00:03Z",
        )


def test_release_workspace_lock_keeps_replaced_same_invocation_file(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    _register_invocation(
        db_path=db_path,
        context=context,
        invocation_id="invocation-lock-1",
    )
    lease = acquire_workspace_root_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lock_key=str(context.workspace_path),
        workspace_root=context.workspace_path,
        execution_session_id=context.execution_session_id,
        tool_invocation_id="invocation-lock-1",
        action_id="action-1",
        acquired_at="2026-03-23T00:00:02Z",
    )
    lease.lock_path.unlink()
    create_workspace_lock_file(
        lock_file=WorkspaceLockFile(
            lock_path=lease.lock_path,
            record=WorkspaceLockRecord(
                lock_id="replacement-lock-id",
                tool_invocation_id="invocation-lock-1",
                execution_session_id=context.execution_session_id,
                lock_key=str(context.workspace_path),
                locked_path=str(context.workspace_path),
                acquired_at="2026-03-23T00:00:03Z",
            ),
        )
    )

    release_workspace_lock(
        db_path=db_path,
        busy_timeout_ms=1_000,
        lease=lease,
        released_at="2026-03-23T00:00:04Z",
    )

    replacement_record = read_workspace_lock_record(lock_path=lease.lock_path)
    assert replacement_record is not None
    assert replacement_record.lock_id == "replacement-lock-id"


def test_workspace_lock_replacement_waits_for_identity_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "runtime.db.workspace-locks" / "workspace.lock.json"
    create_workspace_lock_file(
        lock_file=WorkspaceLockFile(
            lock_path=lock_path,
            record=WorkspaceLockRecord(
                lock_id="old-lock-id",
                tool_invocation_id="old-invocation",
                execution_session_id="session-1",
                lock_key="workspace-1",
                locked_path="/workspace-1",
                acquired_at="2026-03-23T00:00:01Z",
            ),
        )
    )
    # xdist workers are multithreaded, so spawning avoids an unsafe fork.
    monkeypatch.syspath_prepend(str(Path(__file__).parent.parent))
    context = multiprocessing.get_context("spawn")
    read_completed = context.Event()
    continue_removal = context.Event()
    create_started = context.Event()
    create_completed = context.Event()
    remover = context.Process(
        target=_remove_workspace_lock_after_read,
        args=(lock_path, read_completed, continue_removal),
    )
    creator = context.Process(
        target=_create_replacement_workspace_lock,
        args=(lock_path, create_started, create_completed),
    )

    remover.start()
    assert read_completed.wait(timeout=PROCESS_SYNC_TIMEOUT_SECONDS)
    creator.start()
    assert create_started.wait(timeout=PROCESS_SYNC_TIMEOUT_SECONDS)
    assert not create_completed.wait(timeout=0.2)
    continue_removal.set()
    remover.join(timeout=PROCESS_SYNC_TIMEOUT_SECONDS)
    creator.join(timeout=PROCESS_SYNC_TIMEOUT_SECONDS)

    assert remover.exitcode == 0
    assert creator.exitcode == 0
    replacement_record = read_workspace_lock_record(lock_path=lock_path)
    assert replacement_record is not None
    assert replacement_record.lock_id == "new-lock-id"


def test_cleanup_runtime_resource_keeps_replaced_same_invocation_file(
    tmp_path: Path,
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    lock_path = workspace_root_lock_path(
        db_path=db_path,
        lock_key=str(context.workspace_path),
    )
    create_workspace_lock_file(
        lock_file=WorkspaceLockFile(
            lock_path=lock_path,
            record=WorkspaceLockRecord(
                lock_id="replacement-lock-id",
                tool_invocation_id="invocation-lock-1",
                execution_session_id=context.execution_session_id,
                lock_key=str(context.workspace_path),
                locked_path=str(context.workspace_path),
                acquired_at="2026-03-23T00:00:03Z",
            ),
        )
    )

    cleanup_runtime_resource(
        StoredToolRuntimeResource(
            resource_id="resource-lock-1",
            execution_session_id=context.execution_session_id,
            tool_invocation_id="invocation-lock-1",
            action_id="action-1",
            resource_kind="lock",
            status="active",
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(lock_path),
            created_at="2026-03-23T00:00:02Z",
            updated_at="2026-03-23T00:00:02Z",
            cleaned_at=None,
            cleanup_error=None,
            cleanup_attempts=0,
            lock_id="original-lock-id",
        )
    )

    replacement_record = read_workspace_lock_record(lock_path=lock_path)
    assert replacement_record is not None
    assert replacement_record.lock_id == "replacement-lock-id"

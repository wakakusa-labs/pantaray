from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import pantaray_agents.local_runtime.tooling.resources.resource_tracking as resource_tracking
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.repository import complete_execution_session
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    ToolRuntimeResourceSessionConflictError,
)
from pantaray_agents.local_runtime.tooling.resources.resource_tracking import (
    register_path_resource,
    register_process_group_resource,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database
from .resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_bash_invocation,
)


def test_ensure_action_scratch_execution_context_registers_temp_dir_resource(
    tmp_path: Path,
) -> None:
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

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT tool_invocation_id, action_id, resource_kind, status, resource_path
            FROM tool_runtime_resources
            ORDER BY resource_kind ASC
            """
        ).fetchall()

    assert rows == [
        (
            None,
            "action-1",
            "temp_dir",
            "active",
            str(context.action_temp_dir),
        ),
    ]


@pytest.mark.parametrize("conflict", ["terminal_session", "cross_action"])
def test_register_path_resource_requires_live_exact_owner(
    tmp_path: Path, conflict: str
) -> None:
    db_path, context = bootstrap_runtime_db(tmp_path)
    invocation_id = None
    action_id = "action-1"
    if conflict == "terminal_session":
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            execution_session_id=context.execution_session_id,
            status="completed",
            completed_at="2026-03-23T00:00:01Z",
        )
    else:
        invocation_id = register_running_bash_invocation(
            db_path=db_path, context=context
        )
        action_id = "other-action"

    with pytest.raises(ToolRuntimeResourceSessionConflictError):
        register_path_resource(
            db_path=db_path,
            busy_timeout_ms=1_000,
            execution_session_id=context.execution_session_id,
            tool_invocation_id=invocation_id,
            action_id=action_id,
            resource_kind="temp_dir",
            resource_path=context.action_temp_dir / "rejected-resource",
            created_at="2026-03-23T00:00:02Z",
        )

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_runtime_resources"
        ).fetchone() == (1,)


@pytest.mark.parametrize("resource_kind", ["socket", "lock"])
def test_register_path_resource_rejects_unimplemented_resource_producers(
    tmp_path: Path,
    resource_kind: str,
) -> None:
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

    with pytest.raises(
        RuntimeError, match="resource kind requires an explicit producer implementation"
    ):
        register_path_resource(
            db_path=db_path,
            busy_timeout_ms=1_000,
            execution_session_id=context.execution_session_id,
            tool_invocation_id=None,
            action_id="action-1",
            resource_kind=resource_kind,
            resource_path=context.workspace_path / f"{resource_kind}-placeholder",
            created_at="2026-03-23T00:00:01Z",
        )


def test_register_process_group_resource_tracks_even_if_identity_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        resource_tracking.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        resource_tracking,
        "read_process_start_signature",
        lambda pid: (_ for _ in ()).throw(RuntimeError()),
    )

    resource_id = register_process_group_resource(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
        tool_invocation_id=None,
        action_id="action-1",
        pid=9999,
        created_at="2026-03-23T00:00:01Z",
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT resource_id, execution_session_id, tool_invocation_id, resource_kind, status, pid, pgid, process_start_signature
            FROM tool_runtime_resources
            WHERE resource_kind = 'process_group'
            ORDER BY created_at ASC
            """
        ).fetchall()

    assert resource_id is not None
    assert len(rows) == 1
    (
        row_resource_id,
        execution_session_id,
        tool_invocation_id,
        resource_kind,
        status,
        pid,
        pgid,
        signature,
    ) = rows[0]
    assert row_resource_id == resource_id
    assert execution_session_id == context.execution_session_id
    assert tool_invocation_id is None
    assert resource_kind == "process_group"
    assert status == "active"
    assert pid == 9999
    assert pgid is None
    assert signature is None

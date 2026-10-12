from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.models import (
    ExecutionSessionCreateInput,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    create_execution_session_in_connection,
)
from pantaray_agents.local_runtime.tooling.repository.manifests import (
    ensure_action_workspace_manifest_in_connection,
)
from pantaray_agents.local_runtime.tooling.resources.resource_store import (
    create_tool_runtime_resource_in_connection,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database


def _setup_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    insert_agent_action(db_path=db_path)
    return db_path


def _write_context_records(connection: sqlite3.Connection, tmp_path: Path) -> None:
    session_id = "session-1"
    started_at = "2026-08-28T00:00:00Z"
    temp_path = tmp_path / ".runtime-temp" / session_id
    create_execution_session_in_connection(
        connection=connection,
        session=ExecutionSessionCreateInput(
            execution_session_id=session_id,
            user_id="user-1",
            action_id="action-1",
            parent_execution_session_id=None,
            exec_mode="brokered_file_ops",
            cwd_path=str(tmp_path / "scratch"),
            action_temp_dir=str(temp_path),
            app_runtime_python="/app/python",
            network_policy="cloud-proxy-only",
            read_access_scope="workspace",
            capability_snapshot_json={},
            tool_allowlist_json=[],
            status="running",
            started_at=started_at,
            expires_at=None,
        ),
    )
    create_tool_runtime_resource_in_connection(
        connection=connection,
        resource=ToolRuntimeResourceCreateInput(
            resource_id="resource-1",
            execution_session_id=session_id,
            tool_invocation_id=None,
            action_id="action-1",
            resource_kind="temp_dir",
            status="active",
            created_at=started_at,
            pid=None,
            pgid=None,
            process_start_signature=None,
            resource_path=str(temp_path),
        ),
    )
    ensure_action_workspace_manifest_in_connection(
        connection=connection,
        user_id="user-1",
        action_id="action-1",
        execution_session_id=session_id,
        scratch_root_id="root:action-1:scratch",
        manifest_id="manifest:action-1",
        scratch_real_path=str(tmp_path / "scratch"),
        tool_results_real_path=str(tmp_path / "tool-results"),
        memory_folders=(),
        created_at=started_at,
    )


def _record_counts(db_path: Path) -> tuple[int, int, int]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT
                (SELECT COUNT(*) FROM execution_sessions),
                (SELECT COUNT(*) FROM tool_runtime_resources),
                (SELECT COUNT(*) FROM workspace_manifests)
            """
        ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2])


def test_context_helpers_rollback_as_one_caller_transaction(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    with pytest.raises(RuntimeError, match="rollback"):
        with sqlite3.connect(db_path) as connection:
            with immediate_transaction(connection):
                _write_context_records(connection, tmp_path)
                raise RuntimeError("rollback")

    assert _record_counts(db_path) == (0, 0, 0)

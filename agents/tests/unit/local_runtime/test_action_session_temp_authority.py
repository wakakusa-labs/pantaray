from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_session_temp_leaf,
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.models import (
    ExecutionSessionCreateInput,
    ExecutionSessionTerminalStatus,
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import complete_execution_session
from pantaray_agents.local_runtime.tooling.repository.executions import (
    create_execution_session_in_connection,
)
from pantaray_agents.local_runtime.tooling.repository.manifests import (
    ensure_action_workspace_manifest_in_connection,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_authority import (
    load_action_session_temp_cleanup_receipt,
)
from pantaray_agents.local_runtime.tooling.resources.resource_db_support import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling.resources.resource_store import (
    create_tool_runtime_resource_in_connection,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
ACTION_ID = "action-1"
SESSION_ID = "session-1"
STARTED_AT = "2026-08-28T00:00:00Z"


def _seed_evidence(tmp_path: Path, *, status: str = "completed") -> tuple[Path, Path]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
    )
    insert_agent_action(db_path=db_path)
    paths = resolve_action_storage_paths(
        db_path=db_path,
        user_id=USER_ID,
        action_id=ACTION_ID,
    )
    leaf = resolve_action_session_temp_leaf(
        paths=paths,
        execution_session_id=SESSION_ID,
    )
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, BUSY_TIMEOUT_MS)
        with connection:
            create_execution_session_in_connection(
                connection=connection,
                session=_session_input(session_id=SESSION_ID, leaf=leaf),
            )
            create_tool_runtime_resource_in_connection(
                connection=connection,
                resource=ToolRuntimeResourceCreateInput(
                    resource_id="session-root",
                    execution_session_id=SESSION_ID,
                    tool_invocation_id=None,
                    action_id=ACTION_ID,
                    resource_kind="temp_dir",
                    status="active",
                    created_at=STARTED_AT,
                    pid=None,
                    pgid=None,
                    process_start_signature=None,
                    resource_path=str(leaf),
                ),
            )
            ensure_action_workspace_manifest_in_connection(
                connection=connection,
                user_id=USER_ID,
                action_id=ACTION_ID,
                execution_session_id=SESSION_ID,
                scratch_root_id="root:action-1:scratch",
                manifest_id="manifest:action-1",
                scratch_real_path=str(paths.workspace),
                tool_results_real_path=str(paths.tool_results),
                agent_experience_root=None,
                created_at=STARTED_AT,
            )
    if status != "running":
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            execution_session_id=SESSION_ID,
            status=cast(ExecutionSessionTerminalStatus, status),
            completed_at="2026-08-28T00:00:01Z",
        )
    return db_path, leaf


def _session_input(
    *, session_id: str, leaf: Path, parent_id: str | None = None
) -> ExecutionSessionCreateInput:
    return ExecutionSessionCreateInput(
        execution_session_id=session_id,
        user_id=USER_ID,
        action_id=ACTION_ID,
        parent_execution_session_id=parent_id,
        exec_mode="brokered_file_ops",
        cwd_path=str(leaf.parent.parent),
        action_temp_dir=str(leaf),
        app_runtime_python="/app/python",
        network_policy="cloud-proxy-only",
        read_access_scope="workspace",
        capability_snapshot_json={},
        tool_allowlist_json=[],
        status="running",
        started_at=STARTED_AT,
        expires_at=None,
    )


def _load(db_path: Path):
    return load_action_session_temp_cleanup_receipt(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        action_id=ACTION_ID,
        execution_session_id=SESSION_ID,
    )


def _execute(db_path: Path, sql: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(sql)


@pytest.mark.parametrize("status", ["completed", "failed", "canceled", "expired"])
def test_terminal_quiescent_session_receives_frozen_authority(
    tmp_path: Path,
    status: str,
) -> None:
    db_path, leaf = _seed_evidence(tmp_path, status=status)

    assert (receipt := _load(db_path)) is not None
    assert receipt.session_status == status
    assert receipt.canonical_leaf == leaf
    assert receipt.root_resource_id == "session-root"
    assert receipt.root_resource_status == "active"
    assert not leaf.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE execution_sessions SET status = 'running'",
        "UPDATE execution_sessions SET action_temp_dir = '/wrong/session'",
        "UPDATE workspace_manifests SET execution_session_id = NULL",
        "UPDATE workspace_manifests SET status = 'failed'",
        "UPDATE tool_runtime_resources SET resource_path = '/wrong/session'",
        "UPDATE tool_runtime_resources SET action_id = 'action-other'",
        "UPDATE tool_runtime_resources SET status = 'cleaned', cleaned_at = 'now'",
        "INSERT INTO tool_runtime_resources SELECT 'duplicate-root', execution_session_id, NULL, action_id, resource_kind, status, pid, pgid, process_start_signature, resource_path, created_at, updated_at, cleaned_at, cleanup_error, cleanup_attempts, lock_id FROM tool_runtime_resources WHERE resource_id = 'session-root'",
    ],
)
def test_ambiguous_live_or_path_inconsistent_evidence_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    db_path, _ = _seed_evidence(tmp_path)
    _execute(db_path, mutation)

    assert _load(db_path) is None


def test_pending_approval_retains_terminal_session(tmp_path: Path) -> None:
    db_path, _ = _seed_evidence(tmp_path)
    _execute(
        db_path,
        """INSERT INTO approval_sessions(
            approval_session_id, user_id, action_id, manifest_id,
            tool_request_id, tool_id, intent_class, approval_source, status,
            approved_capabilities_json, command_summary_json, requested_at, created_at
        ) VALUES (
            'approval-1', 'user-1', 'action-1', 'manifest:action-1',
            'request-1', 'read', 'read_only', 'prompt', 'pending',
            '[]', '{}', 'now', 'now'
        )""",
    )

    assert _load(db_path) is None
    _execute(
        db_path,
        "UPDATE approval_sessions SET status = 'interrupted', decided_at = 'now'",
    )
    assert _load(db_path) is not None


def test_invocation_and_process_group_must_be_terminal_and_cleaned(
    tmp_path: Path,
) -> None:
    db_path, _ = _seed_evidence(tmp_path)
    _execute(
        db_path,
        """INSERT INTO tool_invocations(
            invocation_id, user_id, action_id, tool_id, manifest_id,
            execution_session_id, intent_class, status, started_at
        ) VALUES (
            'invocation-1', 'user-1', 'action-1', 'read', 'manifest:action-1',
            'session-1', 'read_only', 'running', 'now'
        );
        INSERT INTO tool_runtime_resources(
            resource_id, execution_session_id, tool_invocation_id, action_id,
            resource_kind, status, created_at, updated_at
        ) VALUES (
            'process-1', 'session-1', 'invocation-1', 'action-1',
            'process_group', 'active', 'now', 'now'
        )""",
    )

    assert _load(db_path) is None
    _execute(
        db_path,
        """UPDATE tool_invocations SET status = 'completed', completed_at = 'now';
        UPDATE tool_runtime_resources
        SET status = 'cleaned', cleaned_at = 'now'
        WHERE resource_id = 'process-1'""",
    )
    assert _load(db_path) is not None
    _execute(db_path, "UPDATE tool_invocations SET manifest_id = NULL")
    assert _load(db_path) is None


def test_running_child_session_retains_parent_until_terminal(tmp_path: Path) -> None:
    db_path, leaf = _seed_evidence(tmp_path, status="running")
    child_leaf = leaf.with_name("child-session")
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, BUSY_TIMEOUT_MS)
        with connection:
            create_execution_session_in_connection(
                connection=connection,
                session=_session_input(
                    session_id="child-session",
                    leaf=child_leaf,
                    parent_id=SESSION_ID,
                ),
            )
            connection.execute(
                """INSERT INTO tool_runtime_resources
                SELECT 'child-root', 'child-session', NULL, action_id,
                       resource_kind, status, pid, pgid, process_start_signature,
                       ?, created_at, updated_at, NULL, NULL, 0, lock_id
                FROM tool_runtime_resources WHERE resource_id = 'session-root'""",
                (str(child_leaf),),
            )
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id=SESSION_ID,
        status="completed",
        completed_at="2026-08-28T00:00:01Z",
    )

    assert _load(db_path) is None
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id="child-session",
        status="completed",
        completed_at="2026-08-28T00:00:02Z",
    )
    _execute(
        db_path,
        """UPDATE tool_runtime_resources SET status = 'cleaned', cleaned_at = 'now'
        WHERE resource_id = 'child-root';
        UPDATE workspace_manifests SET execution_session_id = 'child-session'""",
    )
    assert _load(db_path) is not None
    _execute(
        db_path,
        "UPDATE execution_sessions SET action_id = NULL WHERE execution_session_id = 'child-session'",
    )
    assert _load(db_path) is None
    _execute(
        db_path,
        "UPDATE execution_sessions SET action_id = 'action-1', action_temp_dir = '/wrong' WHERE execution_session_id = 'child-session'",
    )
    assert _load(db_path) is None
    _execute(
        db_path,
        "UPDATE execution_sessions SET action_temp_dir = (SELECT resource_path FROM tool_runtime_resources WHERE resource_id = 'child-root') WHERE execution_session_id = 'child-session'",
    )
    _execute(
        db_path,
        "INSERT INTO tool_runtime_resources SELECT 'duplicate-child-root', execution_session_id, NULL, action_id, resource_kind, status, pid, pgid, process_start_signature, resource_path, created_at, updated_at, cleaned_at, cleanup_error, cleanup_attempts, lock_id FROM tool_runtime_resources WHERE resource_id = 'child-root'",
    )
    assert _load(db_path) is None
    _execute(
        db_path,
        "DELETE FROM tool_runtime_resources WHERE resource_id = 'duplicate-child-root'; UPDATE tool_runtime_resources SET resource_path = '/wrong' WHERE resource_id = 'child-root'",
    )
    assert _load(db_path) is None
    _execute(
        db_path, "DELETE FROM tool_runtime_resources WHERE resource_id='child-root'"
    )
    assert _load(db_path) is None


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        _load(db_path)
    assert not db_path.exists()

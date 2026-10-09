import sqlite3
from pathlib import Path

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
    ToolRuntimeResourceCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import complete_execution_session
from pantaray_agents.local_runtime.tooling.repository.executions import (
    create_execution_session_in_connection,
)
from pantaray_agents.local_runtime.tooling.repository.manifests import (
    ensure_action_workspace_manifest_in_connection,
)
from pantaray_agents.local_runtime.tooling.resources import (
    action_session_temp_cleanup as cleanup_module,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_authority import (
    ActionSessionTempCleanupReceipt,
    load_action_session_temp_cleanup_receipt,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    cleanup_action_session_temp,
)
from pantaray_agents.local_runtime.tooling.resources.resource_db_support import (
    configure_connection,
)
from pantaray_agents.local_runtime.tooling.resources.resource_store import (
    create_tool_runtime_resource_in_connection,
    load_tool_runtime_resource,
)
from pantaray_agents.local_runtime.tooling.resources.resource_transition_store import (
    ToolRuntimeResourceTransitionOutcome,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
ACTION_ID = "action-1"
SESSION_ID = "session-1"
STARTED_AT = "2026-08-28T00:00:00Z"


def _seed_cleanup_authority(
    tmp_path: Path,
) -> tuple[Path, Path, ActionSessionTempCleanupReceipt]:
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
    leaf.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, BUSY_TIMEOUT_MS)
        with connection:
            create_execution_session_in_connection(
                connection=connection,
                session=ExecutionSessionCreateInput(
                    execution_session_id=SESSION_ID,
                    user_id=USER_ID,
                    action_id=ACTION_ID,
                    parent_execution_session_id=None,
                    exec_mode="brokered_file_ops",
                    cwd_path=str(paths.workspace),
                    action_temp_dir=str(leaf),
                    app_runtime_python="/app/python",
                    network_policy="cloud-proxy-only",
                    read_access_scope="workspace",
                    capability_snapshot_json={},
                    tool_allowlist_json=[],
                    status="running",
                    started_at=STARTED_AT,
                    expires_at=None,
                ),
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
    complete_execution_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        execution_session_id=SESSION_ID,
        status="completed",
        completed_at="2026-08-28T00:00:01Z",
    )
    receipt = _load_receipt(db_path)
    assert receipt is not None
    return db_path, leaf, receipt


def _load_receipt(db_path: Path) -> ActionSessionTempCleanupReceipt | None:
    return load_action_session_temp_cleanup_receipt(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id=USER_ID,
        action_id=ACTION_ID,
        execution_session_id=SESSION_ID,
    )


def _cleanup(
    db_path: Path, receipt: ActionSessionTempCleanupReceipt
) -> ToolRuntimeResourceTransitionOutcome:
    return cleanup_action_session_temp(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        receipt=receipt,
    )


def test_nested_symlink_is_unlinked_without_following_external_target(
    tmp_path: Path,
) -> None:
    db_path, leaf, receipt = _seed_cleanup_authority(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    nested = leaf / "nested"
    nested.mkdir()
    (nested / "external-link").symlink_to(external, target_is_directory=True)

    outcome = _cleanup(db_path, receipt)

    assert outcome.applied
    assert outcome.current_resource.status == "cleaned"
    assert not leaf.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_physical_delete_before_cas_converges_from_exact_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, leaf, receipt = _seed_cleanup_authority(tmp_path)
    original_transition = cleanup_module.persist_tool_runtime_resource_transition

    def fail_transition(**_kwargs: object) -> None:
        raise RuntimeError("simulated crash before CAS")

    monkeypatch.setattr(
        cleanup_module,
        "persist_tool_runtime_resource_transition",
        fail_transition,
    )
    with pytest.raises(RuntimeError, match="simulated crash before CAS"):
        _cleanup(db_path, receipt)
    assert not leaf.exists()
    monkeypatch.setattr(
        cleanup_module,
        "persist_tool_runtime_resource_transition",
        original_transition,
    )

    retry_receipt = _load_receipt(db_path)
    assert retry_receipt is not None
    outcome = _cleanup(db_path, retry_receipt)

    assert outcome.applied
    assert outcome.current_resource.status == "cleaned"


def test_missing_database_is_not_recreated(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    with pytest.raises(sqlite3.OperationalError):
        load_tool_runtime_resource(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, resource_id="missing"
        )

    assert not db_path.exists()


def test_filesystem_failure_is_durable_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, leaf, receipt = _seed_cleanup_authority(tmp_path)
    original_removal = cleanup_module._remove_session_leaf

    def fail_removal(_receipt: ActionSessionTempCleanupReceipt) -> None:
        raise OSError("filesystem unavailable")

    monkeypatch.setattr(cleanup_module, "_remove_session_leaf", fail_removal)
    failure = _cleanup(db_path, receipt)

    assert failure.applied
    assert failure.current_resource.status == "cleanup_failed"
    assert leaf.exists()
    monkeypatch.setattr(cleanup_module, "_remove_session_leaf", original_removal)
    retry_receipt = _load_receipt(db_path)
    assert retry_receipt is not None
    success = _cleanup(db_path, retry_receipt)

    assert success.applied
    assert success.current_resource.status == "cleaned"
    assert success.current_resource.cleanup_attempts == 1
    assert not leaf.exists()


def test_permanent_filesystem_failure_is_abandoned_at_retry_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, _, _ = _seed_cleanup_authority(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE tool_runtime_resources SET cleanup_attempts = 2")
    receipt = _load_receipt(db_path)
    assert receipt is not None

    def fail_removal(_receipt: ActionSessionTempCleanupReceipt) -> None:
        raise OSError("filesystem unavailable")

    monkeypatch.setattr(cleanup_module, "_remove_session_leaf", fail_removal)
    outcome = _cleanup(db_path, receipt)

    assert outcome.current_resource.status == "abandoned"
    assert outcome.current_resource.cleanup_attempts == 3

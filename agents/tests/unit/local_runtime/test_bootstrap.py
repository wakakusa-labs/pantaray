from __future__ import annotations

import json
import platform
import sqlite3
import sys
from pathlib import Path

import pytest

import pantaray_agents.local_runtime.runtime.local_runtime_generation_cutover as generation_cutover
import pantaray_agents.local_runtime.tooling.bootstrap as tooling_bootstrap
from pantaray_agents.local_runtime.app_runtime_verification import (
    LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    load_node_by_source,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    enqueue_memory_repair,
)
from pantaray_agents.local_runtime.runtime.bootstrap import (
    LLM_PROXY_URL_ENV,
    LOCAL_ARTIFACT_ROOT_ENV,
    LOCAL_DB_BUSY_TIMEOUT_MS_ENV,
    LOCAL_DB_PATH_ENV,
    WEB_TOOLS_PROXY_URL_ENV,
)
from pantaray_agents.local_runtime.runtime.initialization import (
    initialize_local_runtime,
    release_initialized_local_runtime,
)
from pantaray_agents.local_runtime.runtime.local_runtime_generation_cutover import (
    reset_legacy_local_runtime_generation,
)
from pantaray_agents.local_runtime.runtime.process_lock import (
    acquire_runtime_process_lock,
    release_runtime_process_lock,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    HELPER_INSTANCE_ID_ENV,
    MAIN_PROCESS_PID_ENV,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    LOCAL_RUNTIME_WORKSPACE_DIRNAME,
)
from pantaray_agents.local_runtime.tooling.locks.workspace_lock import (
    workspace_lock_store_path,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database
from .migrations.support import _migrations_through

LOCAL_RUNTIME_RESET_MIGRATION_NAME = next(
    migration.name
    for migration in load_default_migrations()
    if migration.version == generation_cutover.LOCAL_RUNTIME_RESET_VERSION
)
PRE_RESET_SCHEMA_VERSION = generation_cutover.LOCAL_RUNTIME_RESET_VERSION - 1


def _initialize_once() -> None:
    initialized = initialize_local_runtime()
    release_initialized_local_runtime(initialized)


def _set_minimum_local_runtime_env(
    monkeypatch: pytest.MonkeyPatch, db_path: Path
) -> None:
    monkeypatch.setenv(LOCAL_DB_PATH_ENV, str(db_path))
    monkeypatch.setenv(LOCAL_DB_BUSY_TIMEOUT_MS_ENV, "1000")
    monkeypatch.setenv(LOCAL_ARTIFACT_ROOT_ENV, str(db_path.parent / "artifacts"))
    monkeypatch.setenv(HELPER_INSTANCE_ID_ENV, "helper-test-instance")
    monkeypatch.setenv(MAIN_PROCESS_PID_ENV, "99999")
    # No Cloud proxy URLs: Electron omits them while account login is disabled.
    monkeypatch.delenv(LLM_PROXY_URL_ENV, raising=False)
    monkeypatch.delenv(WEB_TOOLS_PROXY_URL_ENV, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def _prepare_pre_reset_runtime(
    db_path: Path, *, with_process_group: bool = False
) -> None:
    migrations = _migrations_through(
        load_default_migrations(),
        LOCAL_RUNTIME_RESET_MIGRATION_NAME,
    )
    prepare_test_database(db_path, 1_000, migrations[:-1])
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('legacy-user', 'ja', '2026-08-29T00:00:00Z',
                    '2026-08-29T00:00:00Z')
            """
        )
        if with_process_group:
            connection.execute(
                """
                INSERT INTO tool_runtime_resources(
                    resource_id, execution_session_id, resource_kind, status,
                    pid, pgid, process_start_signature, created_at, updated_at
                ) VALUES ('process-resource', 'legacy-session', 'process_group',
                          'active', 12345, 12345, 'known-start',
                          '2026-08-29T00:00:00Z', '2026-08-29T00:00:00Z')
                """
            )


def test_bootstrap_requires_runtime_env_when_local_runtime_is_always_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(LOCAL_DB_PATH_ENV, raising=False)
    monkeypatch.delenv(LOCAL_DB_BUSY_TIMEOUT_MS_ENV, raising=False)
    monkeypatch.setenv(LOCAL_ARTIFACT_ROOT_ENV, str(tmp_path / "artifacts"))
    monkeypatch.setenv(HELPER_INSTANCE_ID_ENV, "helper-test-instance")
    monkeypatch.setenv(LLM_PROXY_URL_ENV, "https://llm-proxy.example.com")
    monkeypatch.setenv(MAIN_PROCESS_PID_ENV, "99999")
    monkeypatch.setenv(WEB_TOOLS_PROXY_URL_ENV, "https://search-proxy.example.com")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    with pytest.raises(MigrationError, match=LOCAL_DB_PATH_ENV):
        _initialize_once()


@pytest.mark.parametrize(
    "secret_env", ("GEMINI_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY")
)
def test_bootstrap_fails_when_provider_secret_is_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    secret_env: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.setenv(secret_env, "secret")

    with pytest.raises(MigrationError, match="forbidden env"):
        _initialize_once()


@pytest.mark.parametrize(
    ("env_name", "env_value", "error_pattern"),
    (
        (LLM_PROXY_URL_ENV, "ftp://llm-proxy.example.com", LLM_PROXY_URL_ENV),
        (
            WEB_TOOLS_PROXY_URL_ENV,
            "https://user:pass@search-proxy.example.com/v1/search",
            WEB_TOOLS_PROXY_URL_ENV,
        ),
    ),
)
def test_bootstrap_fails_when_cloud_proxy_url_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_name: str,
    env_value: str,
    error_pattern: str,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.setenv(env_name, env_value)

    with pytest.raises(MigrationError, match=error_pattern):
        _initialize_once()


def test_bootstrap_applies_migrations_and_records_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = db_path.parent / "artifacts"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(LOCAL_ARTIFACT_ROOT_ENV, "artifacts")

    _initialize_once()

    assert db_path.exists()
    assert artifact_root.is_dir()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert "repaired_inflight_jobs=" in row[0]


def test_startup_replaces_pre_reset_database_and_all_owned_sidecar_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    workspace_root = tmp_path / LOCAL_RUNTIME_WORKSPACE_DIRNAME
    workspace_locks = workspace_lock_store_path(db_path=db_path)
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    _prepare_pre_reset_runtime(db_path)
    database_sidecars = tuple(
        Path(f"{db_path}{suffix}") for suffix in ("-wal", "-shm", "-journal")
    )
    for sidecar in database_sidecars:
        sidecar.touch()
    legacy_sidecar_inodes = {
        sidecar: sidecar.stat().st_ino for sidecar in database_sidecars
    }

    owned_sentinels = (
        artifact_root / "legacy-artifact.txt",
        workspace_root / "scratch" / "legacy-workspace.txt",
        workspace_locks / "legacy.lock.json",
    )
    for sentinel in owned_sentinels:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("legacy", encoding="utf-8")
    db_backup = tmp_path / "runtime.db.pre-memory-catalog-old.bak"
    artifact_backup = tmp_path / "artifacts.pre-memory-catalog-old.bak"
    db_backup.write_text("legacy", encoding="utf-8")
    artifact_backup.mkdir()
    (artifact_backup / "legacy.txt").write_text("legacy", encoding="utf-8")
    unrelated = tmp_path / "env.local.backend.json"
    unrelated.write_text("preserve", encoding="utf-8")

    _initialize_once()

    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT current_version FROM schema_versions "
            "WHERE component = 'local_runtime'"
        ).fetchone()
        users = connection.execute("SELECT user_id FROM users").fetchall()
        owner = connection.execute("SELECT user_id FROM local_owner").fetchone()
    assert version == (load_default_migrations()[-1].version,)
    assert owner is not None
    assert users == [owner]
    assert not any(sentinel.exists() for sentinel in owned_sentinels)
    assert not db_backup.exists()
    assert not artifact_backup.exists()
    assert all(
        not sidecar.exists() or sidecar.stat().st_ino != legacy_sidecar_inodes[sidecar]
        for sidecar in database_sidecars
    )
    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_fresh_start_unlinks_managed_symlinks_without_following_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    workspace_root = tmp_path / LOCAL_RUNTIME_WORKSPACE_DIRNAME
    workspace_locks = workspace_lock_store_path(db_path=db_path)
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)

    external_targets = tuple(tmp_path / f"external-{index}" for index in range(3))
    for managed_path, external_target in zip(
        (artifact_root, workspace_root, workspace_locks),
        external_targets,
        strict=True,
    ):
        external_target.mkdir()
        (external_target / "preserved.txt").write_text("external", encoding="utf-8")
        managed_path.symlink_to(external_target, target_is_directory=True)

    _initialize_once()

    assert artifact_root.is_dir() and not artifact_root.is_symlink()
    assert all(
        not path.exists() and not path.is_symlink()
        for path in (workspace_root, workspace_locks)
    )
    assert all(
        (target / "preserved.txt").read_text(encoding="utf-8") == "external"
        for target in external_targets
    )


def test_current_generation_preserves_database_and_owned_storage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    prepare_test_database(
        db_path,
        1_000,
        _migrations_through(
            load_default_migrations(),
            LOCAL_RUNTIME_RESET_MIGRATION_NAME,
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES ('current-user', 'ja', '2026-08-29T00:00:00Z',
                    '2026-08-29T00:00:00Z')
            """
        )
    sentinels = (
        artifact_root / "current.txt",
        tmp_path / LOCAL_RUNTIME_WORKSPACE_DIRNAME / "current.txt",
        workspace_lock_store_path(db_path=db_path) / "current.lock.json",
        tmp_path / "runtime.db.pre-memory-catalog-current.bak",
    )
    for sentinel in sentinels:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("current", encoding="utf-8")
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)
    try:
        reset_legacy_local_runtime_generation(
            db_path=db_path,
            busy_timeout_ms=1_000,
            artifact_root=artifact_root,
            runtime_lock=runtime_lock,
        )
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)

    with sqlite3.connect(db_path) as connection:
        users = connection.execute("SELECT user_id FROM users").fetchall()
    assert users == [("current-user",)]
    assert all(path.read_text(encoding="utf-8") == "current" for path in sentinels)


def test_process_group_cleanup_failure_preserves_pre_reset_store_and_aborts_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    _prepare_pre_reset_runtime(db_path, with_process_group=True)
    sentinel = artifact_root / "legacy.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("legacy", encoding="utf-8")

    def fail_cleanup(_resource: object) -> None:
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(generation_cutover, "cleanup_runtime_resource", fail_cleanup)
    with pytest.raises(MigrationError, match="Process-group cleanup failed"):
        _initialize_once()

    with sqlite3.connect(db_path) as connection:
        version = connection.execute(
            "SELECT current_version FROM schema_versions "
            "WHERE component = 'local_runtime'"
        ).fetchone()
    assert version == (PRE_RESET_SCHEMA_VERSION,)
    assert sentinel.read_text(encoding="utf-8") == "legacy"
    release_runtime_process_lock(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path)
    )


def test_invalid_busy_timeout_preserves_pre_reset_store(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    _prepare_pre_reset_runtime(db_path)
    sentinel = artifact_root / "legacy.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("legacy", encoding="utf-8")
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)
    try:
        with pytest.raises(MigrationError, match="positive integer"):
            reset_legacy_local_runtime_generation(
                db_path=db_path,
                busy_timeout_ms=0,
                artifact_root=artifact_root,
                runtime_lock=runtime_lock,
            )
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)

    assert db_path.exists()
    assert sentinel.read_text(encoding="utf-8") == "legacy"


def test_generation_reset_rejects_parent_traversal_before_deletion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app" / "runtime.db"
    sentinel = tmp_path / "unrelated.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    db_path.parent.mkdir()
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)
    try:
        with pytest.raises(MigrationError, match="must not end"):
            reset_legacy_local_runtime_generation(
                db_path=db_path,
                busy_timeout_ms=1_000,
                artifact_root=db_path.parent / "..",
                runtime_lock=runtime_lock,
            )
        lock_alias = (
            runtime_lock.lock_path.parent
            / "unused"
            / ".."
            / runtime_lock.lock_path.name
        )
        with pytest.raises(MigrationError, match="overlap"):
            reset_legacy_local_runtime_generation(
                db_path=db_path,
                busy_timeout_ms=1_000,
                artifact_root=lock_alias,
                runtime_lock=runtime_lock,
            )
        assert runtime_lock.lock_path.exists()
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)

    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_generation_reset_preserves_store_when_schema_marker_is_unreadable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    db_path.write_bytes(b"not a sqlite database")
    sentinel = artifact_root / "preserved.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("preserve", encoding="utf-8")
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)
    try:
        with pytest.raises(MigrationError, match="schema version could not be read"):
            reset_legacy_local_runtime_generation(
                db_path=db_path,
                busy_timeout_ms=1_000,
                artifact_root=artifact_root,
                runtime_lock=runtime_lock,
            )
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)

    assert db_path.read_bytes() == b"not a sqlite database"
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_bootstrap_completes_due_memory_repair_and_records_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    _initialize_once()

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-1', 'ja', '2026-07-18T00:00:00Z',
                        '2026-07-18T00:00:00Z')
                """
            )
            revision = register_inline_domain_memory(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="bootstrap-repair-log",
                content="intact memory",
            )
            node = load_node_by_source(
                connection=connection,
                user_id="user-1",
                source="activity_log",
                source_record_id="bootstrap-repair-log",
            )
            assert node is not None
            enqueue_memory_repair(
                connection=connection,
                user_id="user-1",
                node_id=node.node_id,
                detected_revision_id=revision.revision_id,
                reason="reference_resolution",
            )

    _initialize_once()

    with sqlite3.connect(db_path) as verification:
        repair = verification.execute(
            """
            SELECT state, attempt_count FROM memory_repair_queue
            WHERE user_id = 'user-1'
            """
        ).fetchone()
        recovery = verification.execute(
            "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert repair == ("completed", 0)
    assert recovery is not None
    assert "memory_repairs_completed=1" in recovery[0]


def test_bootstrap_removes_orphaned_memory_workspaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    _initialize_once()

    orphaned_workspace = (
        artifact_root
        / "memory_catalog"
        / "users"
        / "user-1"
        / "workspaces"
        / "run-orphaned"
    )
    draft_path = orphaned_workspace / "draft"
    tool_results_path = orphaned_workspace / "tool-results"
    draft_path.mkdir(parents=True)
    tool_results_path.mkdir()
    (draft_path / "memory.md").write_text("draft", encoding="utf-8")
    (tool_results_path / "output-dead.json").write_text(
        '{"result":"orphaned"}',
        encoding="utf-8",
    )

    _initialize_once()

    assert not orphaned_workspace.exists()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "removed_orphaned_memory_workspaces=1" in row[0]


def test_bootstrap_removes_orphaned_action_tool_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    _initialize_once()
    orphaned_result = (
        tmp_path
        / "local_runtime_workspaces"
        / "scratch"
        / "user-deleted"
        / "action-deleted"
        / "tool-results"
        / "invocation-deleted"
        / f"output-{'a' * 32}.bin"
    )
    orphaned_result.parent.mkdir(parents=True)
    orphaned_result.write_bytes(b"orphaned")

    _initialize_once()

    assert not orphaned_result.exists()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "removed_orphaned_tool_result_files=1" in row[0]
    assert "removed_empty_tool_result_directories=1" in row[0]


def test_bootstrap_starts_when_a_referenced_tool_result_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    _initialize_once()
    insert_agent_action(db_path=db_path)
    tool_results_root = (
        tmp_path
        / "local_runtime_workspaces"
        / "scratch"
        / "user-1"
        / "action-1"
        / "tool-results"
    )
    missing_result = tool_results_root / "step-1" / f"output-{'1' * 32}.json"
    orphaned_result = tool_results_root / "orphan" / f"output-{'2' * 32}.json"
    orphaned_result.parent.mkdir(parents=True)
    orphaned_result.write_bytes(b"orphaned")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, step_type, step_name,
                status, tool_output, created_at
            ) VALUES ('step-1', 'action-1', 'user-1', 1, 'tool_execution',
                      'tool::read', 'success', ?, '2026-03-23T00:00:00Z')
            """,
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "success",
                        "output": {
                            "storage": "action_file",
                            "path": str(missing_result),
                            "media_type": "application/json",
                            "byte_size": 1,
                            "character_count": 1,
                            "line_count": 1,
                        },
                        "output_storage_kind": "action_file",
                        "output_owner_kind": "action_step",
                    }
                ),
            ),
        )

    _initialize_once()

    assert not orphaned_result.exists()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "missing_tool_result_references=1" in row[0]
    assert "removed_orphaned_tool_result_files=1" in row[0]


def test_bootstrap_creates_only_the_logged_out_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)

    _initialize_once()
    _initialize_once()

    with sqlite3.connect(db_path) as connection:
        users = connection.execute("SELECT user_id FROM users").fetchall()
        owner = connection.execute("SELECT user_id FROM local_owner").fetchone()

    assert owner is not None
    assert users == [owner]


def test_initialization_completes_user_erasure_before_catalog_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    artifact_root = tmp_path / "artifacts"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-erased', 'ja', '2026-07-23T00:00:00Z',
                        '2026-07-23T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO activity_logs(
                    log_id, user_id, period_start, period_end, description, status,
                    prompt_name, prompt_version, created_at, updated_at
                ) VALUES (
                    'erased-log', 'user-erased', '2026-07-23T00:00:00Z',
                    '2026-07-23T00:04:00Z', 'Must not be rebuilt.', 'success',
                    'activity_description', '1.0', '2026-07-23T00:04:00Z',
                    '2026-07-23T00:04:00Z'
                )
                """
            )
            register_inline_domain_memory(
                connection=connection,
                user_id="user-erased",
                source="activity_log",
                source_record_id="erased-log",
                content="Must not be rebuilt.",
            )
            connection.execute(
                """
                INSERT INTO memory_artifact_deletions(
                    user_id, deletion_id, artifact_path, reason, state,
                    created_at, updated_at
                ) VALUES (
                    'user-erased', 'erase-1',
                    'memory_catalog/erasure/user-erased-erase-1',
                    'user_erasure', 'planned',
                    '2026-07-23T00:05:00Z', '2026-07-23T00:05:00Z'
                )
                """
            )
    tenant_artifact = (
        artifact_root / "memory_catalog" / "users" / "user-erased" / "memory.md"
    )
    tenant_artifact.parent.mkdir(parents=True)
    tenant_artifact.write_text("erase me", encoding="utf-8")

    _initialize_once()

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM users WHERE user_id = 'user-erased'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM memory_nodes WHERE user_id = 'user-erased'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM memory_artifact_deletions
            WHERE reason = 'user_erasure'
            """
            ).fetchone()[0]
            == 0
        )
        cutover_state = connection.execute(
            "SELECT state FROM memory_catalog_cutovers"
        ).fetchone()
        recovery_note = connection.execute(
            "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0]
    assert cutover_state == ("completed",)
    assert "resumed_user_erasures=1" in recovery_note
    assert not tenant_artifact.exists()


def test_bootstrap_fails_without_artifact_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.delenv(LOCAL_ARTIFACT_ROOT_ENV, raising=False)

    with pytest.raises(MigrationError, match=LOCAL_ARTIFACT_ROOT_ENV):
        _initialize_once()


def test_bootstrap_fails_without_app_runtime_manifest_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.delenv(LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV, raising=False)

    with pytest.raises(MigrationError, match=LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV):
        _initialize_once()


def test_bootstrap_fails_when_app_runtime_hash_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    manifest_path = tmp_path / "app-runtime-manifest.json"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    manifest_path.write_text(
        json.dumps(
            {
                "python_path": str(Path(sys.executable).resolve()),
                "python_version": platform.python_version(),
                "python_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV, str(manifest_path))

    with pytest.raises(MigrationError, match="hash does not match manifest"):
        _initialize_once()


def test_ensure_action_scratch_execution_context_reuses_verified_runtime_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    tooling_bootstrap._resolve_verified_app_runtime_python.cache_clear()
    calls: list[Path] = []

    def _fake_verify() -> Path:
        verified_path = tmp_path / "verified-python"
        calls.append(verified_path)
        return verified_path

    monkeypatch.setattr(
        tooling_bootstrap,
        "load_and_verify_app_runtime_python_from_env",
        _fake_verify,
    )
    insert_agent_action(
        db_path=db_path, action_id="action-1", suggestion_id="suggestion-1"
    )
    insert_agent_action(
        db_path=db_path, action_id="action-2", suggestion_id="suggestion-2"
    )

    first = tooling_bootstrap.ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    second = tooling_bootstrap.ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-2",
        started_at="2026-03-23T00:00:01Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )

    assert len(calls) == 1
    assert first.app_runtime_python == calls[0]
    assert second.app_runtime_python == calls[0]
    tooling_bootstrap._resolve_verified_app_runtime_python.cache_clear()


def test_bootstrap_fails_closed_when_runtime_lock_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    runtime_lock = acquire_runtime_process_lock(db_path=db_path)

    try:
        with pytest.raises(MigrationError, match="LOCAL_RUNTIME_ALREADY_ACTIVE"):
            _initialize_once()
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)


def test_bootstrap_records_subagent_child_recovery_counters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # This note is the only durable evidence the child recovery counters reach.
    db_path = tmp_path / "runtime.db"
    _set_minimum_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(LOCAL_ARTIFACT_ROOT_ENV, "artifacts")

    _initialize_once()

    with sqlite3.connect(db_path) as connection:
        note = str(
            connection.execute(
                "SELECT note FROM runtime_recovery_runs ORDER BY started_at DESC"
            ).fetchone()[0]
        )
    assert "blocked_subagent_children=0" in note
    assert "unreadable_subagent_children=0" in note

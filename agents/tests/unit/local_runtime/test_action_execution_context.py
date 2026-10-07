from __future__ import annotations

import sqlite3
import stat
import sys
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.memory_catalog.lifecycle import (
    plan_revision_garbage_collection,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import bootstrap_local_tooling_catalog
from pantaray_agents.local_runtime.tooling.bootstrap import (
    ActionExecutionContextError,
    ensure_action_scratch_execution_context,
    validate_reusable_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.brokering.manifest_paths import (
    WORKSPACE_PATH_OUTSIDE_ROOTS,
    load_manifest_roots,
    resolve_local_path,
)
from pantaray_agents.local_runtime.tooling.repository import (
    create_workspace_folder,
    delete_workspace_folder,
    load_execution_session,
)
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "user-1",
                    "ja",
                    "2026-03-23T00:00:00Z",
                    "2026-03-23T00:00:00Z",
                ),
            )
    insert_agent_action(db_path=db_path, created_at="2026-03-23T00:00:00Z")
    return db_path


def test_ensure_action_context_snapshots_registered_workspace_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    folder_root = tmp_path / "project"
    folder_root.mkdir()
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    folder = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=folder_root,
        display_name="Project",
        organization_ids=(),
        project_ids=(),
        now="2026-03-23T00:00:00Z",
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash", "run_python"),
    )

    session = load_execution_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        execution_session_id=context.execution_session_id,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        roots = connection.execute(
            """
            SELECT source_type, source_id, real_path, canonical_real_path,
                   can_read, can_apply_patch, can_process_read, can_process_write
            FROM workspace_manifest_roots
            WHERE manifest_id = ?
            ORDER BY source_type ASC
            """,
            (context.manifest_id,),
        ).fetchall()

    assert context.manifest_id == "manifest:action-1"
    assert context.workspace_path.name == "scratch"
    assert context.tool_results_path.name == "tool-results"
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in (
            context.workspace_path.parent,
            context.workspace_path,
            context.tool_results_path,
            context.action_temp_dir,
        )
    )
    assert not context.action_temp_dir.is_relative_to(folder_root.resolve())
    assert session.cwd_path == str(context.workspace_path.resolve())
    assert session.network_policy == "cloud-proxy-only"
    assert context.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
    assert session.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
    assert session.tool_allowlist_json == ["apply_patch", "bash", "read", "run_python"]
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        manifest = connection.execute(
            """
            SELECT scratch_root_path
            FROM workspace_manifests
            WHERE manifest_id = ?
            """,
            (context.manifest_id,),
        ).fetchone()
    scratch_root_path = Path(str(manifest["scratch_root_path"]))
    assert scratch_root_path == context.workspace_path
    assert [(row["source_type"], row["source_id"]) for row in roots].count(
        ("scratch", context.manifest_id)
    ) == 2
    assert ("folder", folder.folder_id) in {
        (row["source_type"], row["source_id"]) for row in roots
    }
    assert str(folder_root.resolve()) in {str(row["real_path"]) for row in roots}
    assert str(folder_root.resolve()) in {
        str(row["canonical_real_path"]) for row in roots
    }
    tool_results_root = next(
        row for row in roots if Path(str(row["real_path"])) == context.tool_results_path
    )
    assert tuple(
        tool_results_root[key]
        for key in (
            "can_read",
            "can_apply_patch",
            "can_process_read",
            "can_process_write",
        )
    ) == (1, 0, 1, 0)


def test_next_action_run_reconciles_registered_folder_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    kept_root = tmp_path / "kept"
    nested_root = kept_root / "nested"
    removed_root = tmp_path / "removed"
    added_root = tmp_path / "added"
    for path in (nested_root, removed_root, added_root):
        path.mkdir(parents=True)

    def register(path: Path, name: str) -> str:
        return create_workspace_folder(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            real_path=path,
            display_name=name,
            organization_ids=(),
            project_ids=(),
            now="2026-03-23T00:00:00Z",
        ).folder_id

    def start_run(started_at: str) -> str:
        return ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at=started_at,
            allowed_tool_ids=("read", "apply_patch"),
        ).manifest_id

    def patch_root_source_id(manifest_id: str, target: Path) -> str | None:
        roots = load_manifest_roots(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            manifest_id=manifest_id,
        )
        resolved = resolve_local_path(
            roots=roots,
            raw_path=str(target),
            cwd_path=tmp_path,
            capability="apply_patch",
            must_exist=False,
        )
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT source_id FROM workspace_manifest_roots WHERE root_id = ?",
                (resolved.root.root_id,),
            ).fetchone()
        return None if row is None else cast(str | None, row[0])

    def root_rows(manifest_id: str, *, folder: bool) -> list[tuple[object, ...]]:
        with sqlite3.connect(db_path) as connection:
            return connection.execute(
                f"""
                SELECT * FROM workspace_manifest_roots
                WHERE manifest_id = ?
                  AND source_type {"=" if folder else "<>"} 'folder'
                ORDER BY root_id
                """,
                (manifest_id,),
            ).fetchall()

    kept_id = register(kept_root, "Kept")
    removed_id = register(removed_root, "Removed")
    nested_id = register(nested_root, "Nested")
    manifest_id = start_run("2026-03-23T00:00:00Z")
    removed_root_id = f"root:{manifest_id}:{removed_id}"
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO file_references(
                    file_reference_id, user_id, action_id, manifest_id, root_id,
                    tool_invocation_id, local_path, canonical_local_path, created_at
                ) VALUES ('ref-1', 'user-1', 'action-1', ?, ?, NULL, ?, ?, ?)
                """,
                (
                    manifest_id,
                    removed_root_id,
                    str(removed_root / "notes.md"),
                    str(removed_root.resolve() / "notes.md"),
                    "2026-03-23T00:00:00Z",
                ),
            )
    other_roots_before = root_rows(manifest_id, folder=False)
    assert patch_root_source_id(manifest_id, removed_root / "a.txt") == removed_id
    with pytest.raises(BrokerPolicyError) as outside:
        patch_root_source_id(manifest_id, added_root / "a.txt")
    assert outside.value.code == WORKSPACE_PATH_OUTSIDE_ROOTS

    delete_workspace_folder(
        db_path=db_path, busy_timeout_ms=1_000, user_id="user-1", folder_id=removed_id
    )
    delete_workspace_folder(
        db_path=db_path, busy_timeout_ms=1_000, user_id="user-1", folder_id=nested_id
    )
    added_id = register(added_root, "Added")
    assert start_run("2026-03-24T00:00:00Z") == manifest_id

    assert patch_root_source_id(manifest_id, added_root / "a.txt") == added_id
    assert patch_root_source_id(manifest_id, nested_root / "a.txt") == kept_id
    with pytest.raises(BrokerPolicyError) as removed:
        patch_root_source_id(manifest_id, removed_root / "a.txt")
    assert removed.value.code == WORKSPACE_PATH_OUTSIDE_ROOTS
    assert root_rows(manifest_id, folder=False) == other_roots_before
    with sqlite3.connect(db_path) as connection:
        reference = connection.execute(
            "SELECT root_id, local_path FROM file_references "
            "WHERE file_reference_id = 'ref-1'"
        ).fetchone()
        snapshot = connection.execute(
            "SELECT workspace_context_snapshot_json FROM workspace_manifests "
            "WHERE manifest_id = ?",
            (manifest_id,),
        ).fetchone()[0]
    assert reference == (None, str(removed_root / "notes.md"))
    assert str(added_root) in snapshot
    assert str(removed_root) not in snapshot

    folder_roots_after_refresh = root_rows(manifest_id, folder=True)
    start_run("2026-03-25T00:00:00Z")
    assert root_rows(manifest_id, folder=True) == folder_roots_after_refresh


def test_action_storage_layout_uses_canonical_db_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    db_path = _bootstrap_db(real_parent)
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )

    context = ensure_action_scratch_execution_context(
        db_path=alias_parent / db_path.name,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read",),
    )

    assert context.workspace_path == context.workspace_path.resolve()
    assert context.tool_results_path == context.tool_results_path.resolve()
    assert context.action_temp_dir == context.action_temp_dir.resolve()
    assert real_parent in context.workspace_path.parents


def test_action_storage_layout_upgrades_existing_managed_directories_to_owner_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read",),
    )
    managed_paths = (
        tmp_path / "local_runtime_workspaces",
        tmp_path / "local_runtime_workspaces" / "scratch",
        context.workspace_path.parent.parent,
        context.workspace_path.parent,
        context.workspace_path,
        context.tool_results_path,
        context.action_temp_dir.parent,
    )
    for path in managed_paths:
        path.chmod(0o755)

    next_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-24T00:00:00Z",
        allowed_tool_ids=("read",),
    )

    assert context.action_temp_dir != next_context.action_temp_dir
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in (*managed_paths, next_context.action_temp_dir)
    )


@pytest.mark.parametrize(
    ("user_id", "action_id"),
    (
        ("..", "action-1"),
        ("nested/user", "action-1"),
        ("user-1", "nested/action"),
        ("user\\name", "action-1"),
        ("user-1", "action\0id"),
    ),
)
def test_action_storage_layout_rejects_path_identifiers_before_db_writes(
    tmp_path: Path,
    user_id: str,
    action_id: str,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    with pytest.raises(
        ActionExecutionContextError,
        match="must be a non-path identifier",
    ):
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id=user_id,
            action_id=action_id,
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )

    assert _execution_storage_row_counts(db_path) == (0, 0)


def test_action_storage_layout_rejects_symlink_before_db_writes(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    scratch_root = tmp_path / "local_runtime_workspaces" / "scratch"
    scratch_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (scratch_root / "user-1").symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        ActionExecutionContextError,
        match="failed to create the durable Action storage layout",
    ):
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )

    assert not (outside / "action-1").exists()
    assert _execution_storage_row_counts(db_path) == (0, 0)


def test_action_storage_layout_fsync_failure_precedes_db_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)

    def fail_directory_fsync(_directory_fd: int) -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(bootstrap_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(
        ActionExecutionContextError,
        match="failed to create the durable Action storage layout",
    ):
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )

    assert _execution_storage_row_counts(db_path) == (0, 0)


def test_action_context_commit_precedes_session_leaf_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    class SimulatedCrash(BaseException):
        pass

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_materialize_action_session_temp_leaf",
        lambda **_kwargs: (_ for _ in ()).throw(SimulatedCrash()),
    )

    with pytest.raises(SimulatedCrash):
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )

    session_id, session_status, temp_path, manifest_session_id, resource_status = (
        _load_root_context_record(db_path)
    )
    assert session_status == "running"
    assert manifest_session_id == session_id
    assert resource_status == "active"
    assert not Path(temp_path).exists()


def test_action_context_leaf_failure_retains_cleanup_intent_and_fails_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_materialize_action_session_temp_leaf",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("leaf unavailable")),
    )

    with pytest.raises(ActionExecutionContextError) as error:
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )

    session_id, session_status, temp_path, manifest_session_id, resource_status = (
        _load_root_context_record(db_path)
    )
    assert isinstance(error.value.__cause__, OSError)
    assert session_status == "failed"
    assert manifest_session_id == session_id
    assert resource_status == "active"
    assert not Path(temp_path).exists()


def test_retained_action_context_requires_exact_session_leaf_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read",),
    )
    retained_context = {
        "db_path": db_path,
        "busy_timeout_ms": 1_000,
        "user_id": "user-1",
        "action_id": "action-1",
        "manifest_id": context.manifest_id,
        "execution_session_id": context.execution_session_id,
        "execution_network_policy": context.network_policy,
        "action_temp_dir": str(context.action_temp_dir),
        "app_runtime_python": str(context.app_runtime_python),
        "read_access_scope": context.read_access_scope,
    }

    validate_reusable_action_scratch_execution_context(**retained_context)

    with pytest.raises(ActionExecutionContextError, match="noncanonical"):
        validate_reusable_action_scratch_execution_context(
            **{
                **retained_context,
                "action_temp_dir": str(context.action_temp_dir.parent),
            }
        )
    assert _execution_storage_row_counts(db_path) == (1, 1)


def _execution_storage_row_counts(db_path: Path) -> tuple[int, int]:
    with sqlite3.connect(db_path) as connection:
        execution_count = int(
            connection.execute("SELECT COUNT(*) FROM execution_sessions").fetchone()[0]
        )
        manifest_count = int(
            connection.execute("SELECT COUNT(*) FROM workspace_manifests").fetchone()[0]
        )
    return execution_count, manifest_count


def _load_root_context_record(db_path: Path) -> tuple[str, str, str, str, str]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT session.execution_session_id, session.status,
                   session.action_temp_dir, manifest.execution_session_id,
                   resource.status
            FROM execution_sessions AS session
            JOIN workspace_manifests AS manifest
              ON manifest.action_id = session.action_id
            JOIN tool_runtime_resources AS resource
              ON resource.execution_session_id = session.execution_session_id
             AND resource.resource_kind = 'temp_dir'
             AND resource.tool_invocation_id IS NULL
            """
        ).fetchone()
    assert row is not None
    return cast(tuple[str, str, str, str, str], row)


def test_action_manifest_pins_current_agent_experience_revision_read_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.nodes.workspace_mount_catalog import (  # noqa: E501
        load_required_local_workspace_manifest_catalog,
    )
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    first_root = _activate_agent_experience_revision(
        db_path=db_path,
        artifact_root=artifact_root,
        revision_id="experience-revision-1",
        artifact_root_path="memory/experience-revision-1",
        create_node=True,
    )

    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash", "run_python"),
    )
    _activate_agent_experience_revision(
        db_path=db_path,
        artifact_root=artifact_root,
        revision_id="experience-revision-2",
        artifact_root_path="memory/experience-revision-2",
        create_node=False,
    )
    resumed_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-24T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash", "run_python"),
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT source_id, real_path, can_read, can_apply_patch,
                   can_process_read, can_process_write
            FROM workspace_manifest_roots
            WHERE manifest_id = ? AND source_type = 'agent_experience'
            """,
            (context.manifest_id,),
        ).fetchone()
    catalog = load_required_local_workspace_manifest_catalog(
        user_id="user-1",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    )
    experience_line = next(
        line for line in catalog.root_catalog.splitlines() if str(first_root) in line
    )

    assert resumed_context.manifest_id == context.manifest_id
    assert row is not None
    assert row["source_id"] == "experience-revision-1"
    assert row["real_path"] == str(first_root)
    assert tuple(
        row[key]
        for key in (
            "can_read",
            "can_apply_patch",
            "can_process_read",
            "can_process_write",
        )
    ) == (1, 0, 0, 0)
    assert experience_line == (
        f"- `{first_root}`: useful past procedures may exist here; "
        "explore only when needed"
    )


def test_action_manifest_rejects_healthy_legacy_inline_agent_experience(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO memory_nodes(
                    user_id, node_id, source_type, source_record_id,
                    lifecycle, integrity, current_revision_id,
                    created_at, updated_at
                ) VALUES (
                    'user-1', 'legacy-experience', 'agent_experience', 'user-1',
                    'preparing', 'healthy', NULL,
                    '2026-03-23T00:00:00Z', '2026-03-23T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO memory_revisions(
                    user_id, revision_id, node_id, body_kind, inline_body,
                    artifact_root_path, fragment_schema_version,
                    content_sha256, created_at
                ) VALUES (
                    'user-1', 'legacy-inline-revision', 'legacy-experience',
                    'inline', '# Legacy', NULL, 1, 'legacy',
                    '2026-03-23T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                UPDATE memory_nodes
                SET lifecycle = 'active', current_revision_id = 'legacy-inline-revision'
                WHERE user_id = 'user-1' AND node_id = 'legacy-experience'
                """
            )

    with pytest.raises(MigrationError, match="not an artifact tree"):
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )


def test_action_manifest_fails_when_healthy_agent_experience_index_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    experience_root = _activate_agent_experience_revision(
        db_path=db_path,
        artifact_root=artifact_root,
        revision_id="experience-revision-missing-index",
        artifact_root_path="memory/experience-revision-missing-index",
        create_node=True,
    )
    (experience_root / "index.md").unlink()
    with pytest.raises(MigrationError, match="healthy agent experience.*index"):
        ensure_action_scratch_execution_context(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            action_id="action-1",
            started_at="2026-03-23T00:00:00Z",
            allowed_tool_ids=("read",),
        )


def test_processing_action_manifest_leases_agent_experience_revision_from_gc(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module

    db_path = _bootstrap_db(tmp_path)
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setenv("LOCAL_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    _activate_agent_experience_revision(
        db_path=db_path,
        artifact_root=artifact_root,
        revision_id="experience-revision-leased",
        artifact_root_path="memory/experience-revision-leased",
        create_node=True,
    )
    ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read",),
    )
    _activate_agent_experience_revision(
        db_path=db_path,
        artifact_root=artifact_root,
        revision_id="experience-revision-current",
        artifact_root_path="memory/experience-revision-current",
        create_node=False,
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with connection:
            assert (
                plan_revision_garbage_collection(
                    connection=connection,
                    user_id="user-1",
                    revision_id="experience-revision-leased",
                )
                is None
            )
            connection.execute(
                "UPDATE agent_actions SET status = 'error' WHERE action_id = 'action-1'"
            )
            deletion_id = plan_revision_garbage_collection(
                connection=connection,
                user_id="user-1",
                revision_id="experience-revision-leased",
            )

    assert deletion_id is not None


def _activate_agent_experience_revision(
    *,
    db_path: Path,
    artifact_root: Path,
    revision_id: str,
    artifact_root_path: str,
    create_node: bool,
) -> Path:
    experience_root = artifact_root / artifact_root_path / "agent_experience"
    experience_root.mkdir(parents=True)
    (experience_root / "entries").mkdir()
    (experience_root / "index.md").write_text("# Agent Experience\n", encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        with connection:
            if create_node:
                connection.execute(
                    """
                    INSERT INTO memory_nodes(
                        user_id, node_id, source_type, source_record_id,
                        lifecycle, integrity, current_revision_id,
                        created_at, updated_at
                    ) VALUES (
                        'user-1', 'agent-experience-collection',
                        'agent_experience', 'user-1', 'preparing', 'healthy', NULL,
                        '2026-03-23T00:00:00Z', '2026-03-23T00:00:00Z'
                    )
                    """
                )
            connection.execute(
                """
                INSERT INTO memory_revisions(
                    user_id, revision_id, node_id, body_kind, inline_body,
                    artifact_root_path, fragment_schema_version,
                    content_sha256, created_at
                ) VALUES (
                    'user-1', ?, 'agent-experience-collection', 'artifact_tree', NULL,
                    ?, 1, ?, '2026-03-23T00:00:00Z'
                )
                """,
                (revision_id, artifact_root_path, revision_id),
            )
            connection.execute(
                """
                UPDATE memory_nodes
                SET lifecycle = 'active', current_revision_id = ?,
                    updated_at = '2026-03-23T00:00:00Z'
                WHERE user_id = 'user-1'
                  AND node_id = 'agent-experience-collection'
                """,
                (revision_id,),
            )
    return experience_root.resolve()

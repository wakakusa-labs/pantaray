from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.workspace_mount_catalog import (
    load_required_local_workspace_manifest_catalog,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_project_order import (
    replace_workspace_project_order,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
    create_workspace_folder,
    create_workspace_organization,
    create_workspace_project,
    list_workspace_settings,
    replace_workspace_folder_links,
    replace_workspace_project_links,
    update_read_access_scope,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_deletion import (
    delete_workspace_folder,
    delete_workspace_organization,
    delete_workspace_project,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database

TIMESTAMP = "2026-03-23T00:00:00Z"


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("user-1", "ja", TIMESTAMP, TIMESTAMP),
        )
    insert_agent_action(db_path=db_path, created_at=TIMESTAMP)
    return db_path


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    row = connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()
    if row is None:
        raise AssertionError(f"table count did not return a row: {table_name}")
    return int(row[0])


def test_workspace_settings_crud_and_duplicate_folder_update(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    folder_path = tmp_path / "repo"
    folder_path.mkdir()

    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Acme",
        now=TIMESTAMP,
    )
    project = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Core",
        organization_ids=(organization.organization_id,),
        now=TIMESTAMP,
    )
    folder = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=folder_path,
        display_name="Repo",
        organization_ids=(organization.organization_id,),
        project_ids=(project.project_id,),
        now=TIMESTAMP,
    )
    duplicate = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=folder_path,
        display_name="Renamed Repo",
        organization_ids=(),
        project_ids=(),
        now="2026-03-24T00:00:00Z",
    )

    settings = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )

    assert duplicate.folder_id == folder.folder_id
    assert duplicate.canonical_real_path == folder.canonical_real_path
    assert [item.display_name for item in settings.folders] == ["Renamed Repo"]
    assert settings.folders[0].organization_ids == ()
    assert settings.folders[0].project_ids == ()
    assert settings.organizations[0].display_name == "Acme"
    assert settings.projects[0].display_name == "Core"
    assert settings.projects[0].organization_ids == (organization.organization_id,)
    assert settings.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS


def test_workspace_settings_deletes_entities_and_cascades_links(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    folder_path = tmp_path / "repo"
    folder_path.mkdir()

    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Acme",
        now=TIMESTAMP,
    )
    project = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Core",
        organization_ids=(organization.organization_id,),
        now=TIMESTAMP,
    )
    folder = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=folder_path,
        display_name="Repo",
        organization_ids=(organization.organization_id,),
        project_ids=(project.project_id,),
        now=TIMESTAMP,
    )

    delete_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        project_id=project.project_id,
    )

    settings_after_project_delete = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )
    assert settings_after_project_delete.projects == ()
    assert settings_after_project_delete.folders[0].project_ids == ()

    delete_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        organization_id=organization.organization_id,
    )

    settings_after_organization_delete = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )
    assert settings_after_organization_delete.organizations == ()
    assert settings_after_organization_delete.folders[0].organization_ids == ()

    delete_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        folder_id=folder.folder_id,
    )

    settings_after_folder_delete = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )
    assert settings_after_folder_delete.folders == ()

    with sqlite3.connect(db_path) as connection:
        assert _table_count(connection, "workspace_projects") == 0
        assert _table_count(connection, "workspace_organizations") == 0
        assert _table_count(connection, "workspace_folders") == 0
        assert _table_count(connection, "workspace_project_organizations") == 0
        assert _table_count(connection, "workspace_folder_organizations") == 0
        assert _table_count(connection, "workspace_folder_projects") == 0


def test_workspace_settings_persists_read_access_scope(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)

    updated = update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
        now=TIMESTAMP,
    )
    settings = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )

    assert updated == READ_ACCESS_SCOPE_WORKSPACE
    assert settings.read_access_scope == READ_ACCESS_SCOPE_WORKSPACE


def test_workspace_settings_supports_multiple_context_links(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    folder_path = tmp_path / "research"
    folder_path.mkdir()

    org_a = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org A",
        now=TIMESTAMP,
    )
    org_b = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org B",
        now=TIMESTAMP,
    )
    project_a = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project A",
        organization_ids=(org_a.organization_id, org_b.organization_id),
        now=TIMESTAMP,
    )
    project_b = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project B",
        organization_ids=(),
        now=TIMESTAMP,
    )

    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=folder_path,
        display_name="Research",
        organization_ids=(org_b.organization_id, org_a.organization_id),
        project_ids=(project_b.project_id, project_a.project_id),
        now=TIMESTAMP,
    )

    settings = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    )

    projects = {project.display_name: project for project in settings.projects}
    assert set(projects["Project A"].organization_ids) == {
        org_a.organization_id,
        org_b.organization_id,
    }
    assert settings.folders[0].organization_ids == ()
    assert set(settings.folders[0].project_ids) == {
        project_a.project_id,
        project_b.project_id,
    }


def test_workspace_settings_replaces_existing_context_links(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    folder_path = tmp_path / "repo"
    folder_path.mkdir()

    org_a = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org A",
        now=TIMESTAMP,
    )
    org_b = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org B",
        now=TIMESTAMP,
    )
    project = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project",
        organization_ids=(org_a.organization_id,),
        now=TIMESTAMP,
    )
    folder = create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=folder_path,
        display_name="Repo",
        organization_ids=(org_a.organization_id,),
        project_ids=(),
        now=TIMESTAMP,
    )

    updated_project = replace_workspace_project_links(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        project_id=project.project_id,
        organization_ids=(org_b.organization_id,),
        now="2026-03-24T00:00:00Z",
    )
    updated_folder = replace_workspace_folder_links(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        folder_id=folder.folder_id,
        organization_ids=(org_b.organization_id,),
        project_ids=(project.project_id,),
        now="2026-03-24T00:00:00Z",
    )

    assert updated_project.organization_ids == (org_b.organization_id,)
    assert updated_folder.organization_ids == ()
    assert updated_folder.project_ids == (project.project_id,)
    with sqlite3.connect(db_path) as connection:
        direct_link_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM workspace_folder_organizations
            WHERE folder_id = ?
            """,
            (folder.folder_id,),
        ).fetchone()
    assert direct_link_count == (0,)


def test_workspace_project_order_is_contiguous_and_new_projects_append(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    project_a = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Zulu",
        organization_ids=(),
        now=TIMESTAMP,
    )
    project_b = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Alpha",
        organization_ids=(),
        now=TIMESTAMP,
    )

    replace_workspace_project_order(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        project_ids=(project_b.project_id, project_a.project_id),
        now="2026-08-08T01:00:00Z",
    )
    project_c = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Middle",
        organization_ids=(),
        now="2026-08-08T02:00:00Z",
    )

    projects = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    ).projects
    assert [(project.project_id, project.sort_order) for project in projects] == [
        (project_b.project_id, 0),
        (project_a.project_id, 1),
        (project_c.project_id, 2),
    ]


@pytest.mark.parametrize(
    "submitted_ids",
    [
        ("project-a", "project-a"),
        ("project-a",),
        ("project-a", "unknown-project"),
        ("project-a", "other-project"),
    ],
)
def test_workspace_project_order_rejects_incomplete_or_foreign_sets(
    tmp_path: Path,
    submitted_ids: tuple[str, ...],
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _create_project_with_id(db_path, "user-1", "project-a", "A")
    _create_project_with_id(db_path, "user-1", "project-b", "B")
    _insert_user_and_project(db_path, "user-2", "other-project", "Other")

    with pytest.raises(
        MigrationError,
        match="must contain every active project exactly once",
    ):
        replace_workspace_project_order(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            project_ids=submitted_ids,
            now="2026-08-08T01:00:00Z",
        )

    projects = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    ).projects
    assert [project.project_id for project in projects] == ["project-a", "project-b"]


def test_workspace_project_order_rolls_back_all_updates_on_failure(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _create_project_with_id(db_path, "user-1", "project-a", "A")
    _create_project_with_id(db_path, "user-1", "project-b", "B")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_project_a_order
            BEFORE UPDATE OF sort_order ON workspace_projects
            WHEN NEW.project_id = 'project-a'
            BEGIN
                SELECT RAISE(ABORT, 'forced project order failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced project order failure"):
        replace_workspace_project_order(
            db_path=db_path,
            busy_timeout_ms=1_000,
            user_id="user-1",
            project_ids=("project-b", "project-a"),
            now="2026-08-08T01:00:00Z",
        )

    projects = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
    ).projects
    assert [(project.project_id, project.sort_order) for project in projects] == [
        ("project-a", 0),
        ("project-b", 1),
    ]


def _create_project_with_id(
    db_path: Path,
    user_id: str,
    project_id: str,
    display_name: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO workspace_projects(
                project_id, user_id, display_name, sort_order, created_at, updated_at
            ) VALUES (
                ?, ?, ?,
                COALESCE((
                    SELECT MAX(sort_order) + 1
                    FROM workspace_projects
                    WHERE user_id = ? AND status = 'active'
                ), 0),
                ?, ?
            )
            """,
            (project_id, user_id, display_name, user_id, TIMESTAMP, TIMESTAMP),
        )


def _insert_user_and_project(
    db_path: Path,
    user_id: str,
    project_id: str,
    display_name: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES (?, 'ja', ?, ?)
            """,
            (user_id, TIMESTAMP, TIMESTAMP),
        )
    _create_project_with_id(db_path, user_id, project_id, display_name)


def test_workspace_manifest_catalog_uses_manifest_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module
    from pantaray_agents.local_runtime.tooling.bootstrap import (
        ensure_action_scratch_execution_context,
    )

    db_path = _bootstrap_db(tmp_path)
    repo_z_path = tmp_path / "repo-z"
    repo_a_path = tmp_path / "repo-a"
    repo_z_path.mkdir()
    repo_a_path.mkdir()
    monkeypatch.setenv("LOCAL_DB_PATH", str(db_path))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    organization = create_workspace_organization(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Org A",
        now=TIMESTAMP,
    )
    project_z = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project Z",
        organization_ids=(organization.organization_id,),
        now=TIMESTAMP,
    )
    project_a = create_workspace_project(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        display_name="Project A",
        organization_ids=(organization.organization_id,),
        now=TIMESTAMP,
    )
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_z_path,
        display_name="Repo Z",
        organization_ids=(organization.organization_id,),
        project_ids=(project_z.project_id,),
        now=TIMESTAMP,
    )
    create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=repo_a_path,
        display_name="Repo A",
        organization_ids=(organization.organization_id,),
        project_ids=(project_a.project_id,),
        now=TIMESTAMP,
    )
    replace_workspace_project_order(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        project_ids=(project_z.project_id, project_a.project_id),
        now="2026-08-08T01:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at=TIMESTAMP,
        allowed_tool_ids=("read", "apply_patch", "bash", "run_python"),
    )

    catalog = load_required_local_workspace_manifest_catalog(
        user_id="user-1",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
    )

    assert f"`.` = `{context.workspace_path}`" in catalog.root_catalog
    insert_agent_action(
        db_path=db_path,
        action_id="other-action",
        suggestion_id="other-suggestion",
        created_at=TIMESTAMP,
    )
    other_context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="other-action",
        started_at=TIMESTAMP,
        allowed_tool_ids=("read",),
    )
    with pytest.raises(
        RuntimeError, match="manifest and execution session do not match"
    ):
        load_required_local_workspace_manifest_catalog(
            user_id="user-1",
            manifest_id=context.manifest_id,
            execution_session_id=other_context.execution_session_id,
            read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
        )
    assert str(repo_z_path.resolve()) in catalog.root_catalog
    assert str(repo_a_path.resolve()) in catalog.root_catalog
    assert "Repo A" in catalog.root_catalog
    assert "Org A" not in catalog.root_catalog
    assert "Project A" not in catalog.root_catalog
    assert [
        organization.display_name
        for organization in catalog.workspace_context.organizations
    ] == ["Org A"]
    assert [project.display_name for project in catalog.workspace_context.projects] == [
        "Project Z",
        "Project A",
    ]

    with sqlite3.connect(db_path) as connection:
        root_columns = [
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(workspace_manifest_roots)"
            ).fetchall()
        ]
        snapshot_row = connection.execute(
            """
            SELECT workspace_context_snapshot_json
            FROM workspace_manifests
            WHERE manifest_id = ?
            """,
            (context.manifest_id,),
        ).fetchone()
        root_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT display_name
                FROM workspace_manifest_roots
                WHERE manifest_id = ? AND source_type = 'folder'
                ORDER BY rowid
                """,
                (context.manifest_id,),
            ).fetchall()
        ]
    assert "organization_names_json" not in root_columns
    assert "project_names_json" not in root_columns
    assert snapshot_row is not None
    assert root_names == ["Repo A", "Repo Z"]
    assert '"display_name": "Org A"' in snapshot_row[0]
    assert '"display_name": "Project A"' in snapshot_row[0]
    assert '"display_name": "Project Z"' in snapshot_row[0]
    assert '"display_name": "Repo A"' in snapshot_row[0]

"""A registered folder no project holds must come out of the migration inside a project.

These use the production migration list rather than `support.load_default_migrations`,
which stops below the reset version and would never reach this migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import (
    MigrationSpec,
    apply_migrations,
)
from pantaray_agents.local_runtime.storage.migrations.specs import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    list_workspace_settings,
)

BUSY_TIMEOUT_MS = 1_000
MIGRATION_VERSION = 125
TIMESTAMP = "2026-10-01T00:00:00Z"


def _migrations(*, through: bool) -> tuple[MigrationSpec, ...]:
    return tuple(
        migration
        for migration in load_default_migrations()
        if migration.version < MIGRATION_VERSION
        or (through and migration.version == MIGRATION_VERSION)
    )


def test_every_unassigned_folder_ends_up_in_a_project(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations(through=False),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for user_id in ("user-1", "user-2"):
            connection.execute(
                "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
                " VALUES (?, 'ja', ?, ?)",
                (user_id, TIMESTAMP, TIMESTAMP),
            )
        connection.executemany(
            "INSERT INTO workspace_organizations(organization_id, user_id, display_name,"
            " status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            [
                ("org-b", "user-1", "B", TIMESTAMP, TIMESTAMP),
                ("org-a", "user-1", "A", TIMESTAMP, TIMESTAMP),
            ],
        )
        connection.executemany(
            "INSERT INTO workspace_projects(project_id, user_id, display_name, sort_order,"
            " status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
            [
                ("project-web", "user-1", "web", 0, TIMESTAMP, TIMESTAMP),
                ("project-docs", "user-1", "docs", 1, TIMESTAMP, TIMESTAMP),
            ],
        )
        connection.executemany(
            "INSERT INTO workspace_folders(folder_id, user_id, display_name,"
            " canonical_real_path, real_path, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            [
                (
                    folder_id,
                    user_id,
                    name,
                    f"/{user_id}/{folder_id}",
                    f"/{user_id}/{folder_id}",
                    TIMESTAMP,
                    TIMESTAMP,
                )
                for folder_id, user_id, name in (
                    ("held", "user-1", "held"),
                    ("api", "user-1", "api"),
                    ("api-copy", "user-1", "api"),
                    ("docs", "user-1", "docs"),
                    ("multi", "user-1", "multi"),
                    ("other", "user-2", "other"),
                )
            ],
        )
        connection.execute(
            "INSERT INTO workspace_folder_projects(user_id, folder_id, project_id, created_at)"
            " VALUES ('user-1', 'held', 'project-web', ?)",
            (TIMESTAMP,),
        )
        connection.execute(
            "INSERT INTO workspace_project_organizations(user_id, project_id,"
            " organization_id, created_at) VALUES ('user-1', 'project-docs', 'org-a', ?)",
            (TIMESTAMP,),
        )
        connection.executemany(
            "INSERT INTO workspace_folder_organizations(user_id, folder_id, organization_id,"
            " created_at) VALUES ('user-1', ?, ?, ?)",
            [
                ("api", "org-b", TIMESTAMP),
                ("api-copy", "org-a", TIMESTAMP),
                ("docs", "org-b", TIMESTAMP),
                ("multi", "org-a", TIMESTAMP),
                ("multi", "org-b", TIMESTAMP),
            ],
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations(through=True),
    )

    user_1 = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, user_id="user-1"
    )
    projects = {project.display_name: project for project in user_1.projects}
    # Each folder gets a project of its own, numbered past the names already taken, and the
    # projects the user had keep their folders and organizations.
    assert [
        (project.display_name, project.sort_order) for project in user_1.projects
    ] == [
        ("web", 0),
        ("docs", 1),
        ("api", 2),
        ("api 2", 3),
        ("docs 2", 4),
        ("multi", 5),
    ]
    assert {folder.folder_id: folder.project_ids for folder in user_1.folders} == {
        "held": ("project-web",),
        "api": (projects["api"].project_id,),
        "api-copy": (projects["api 2"].project_id,),
        "docs": (projects["docs 2"].project_id,),
        "multi": (projects["multi"].project_id,),
    }
    assert {name: project.organization_ids for name, project in projects.items()} == {
        "web": (),
        "docs": ("org-a",),
        "api": ("org-b",),
        "api 2": ("org-a",),
        "docs 2": ("org-b",),
        "multi": ("org-a", "org-b"),
    }
    assert all(folder.organization_ids == () for folder in user_1.folders)
    user_2 = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, user_id="user-2"
    )
    assert [
        (project.display_name, project.sort_order) for project in user_2.projects
    ] == [("other", 0)]
    assert user_2.folders[0].project_ids == (user_2.projects[0].project_id,)
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_folder_organizations"
            ).fetchone()[0]
            == 0
        )

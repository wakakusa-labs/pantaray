from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .support import _insert_user, _migrations_before, apply_migrations

_INSERT = (
    "INSERT INTO todo_items(todo_id, user_id, project_id, organization_id, text, "
    "author, origin_kind, created_at) "
    "VALUES (?, 'user-1', ?, ?, 'Send', 'user', 'written', 't')"
)


def test_stored_todos_keep_their_project_and_gain_no_organization_of_their_own(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    before = _migrations_before(migrations, "0141_todo_organization.sql")
    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=before)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.executescript(
            "INSERT INTO workspace_organizations(organization_id, user_id, "
            "display_name, created_at, updated_at) VALUES ('org-a', 'user-1', 'A', "
            "'t', 't');"
            "INSERT INTO workspace_projects(project_id, user_id, display_name, "
            "created_at, updated_at) VALUES ('p-1', 'user-1', 'P', 't', 't');"
            "INSERT INTO todo_items(todo_id, user_id, project_id, text, author, "
            "origin_kind, created_at) VALUES ('t-1', 'user-1', 'p-1', 'Send', "
            "'user', 'written', 't');"
        )

    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=migrations)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT todo_id, project_id, organization_id FROM todo_items"
        ).fetchall() == [("t-1", "p-1", None)]
        connection.execute(_INSERT, ("t-2", None, "org-a"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(_INSERT, ("t-3", "p-1", "org-a"))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(_INSERT, ("t-4", None, "missing"))

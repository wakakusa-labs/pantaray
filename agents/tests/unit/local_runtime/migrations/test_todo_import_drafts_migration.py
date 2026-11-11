from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .support import _insert_user, _migrations_before, apply_migrations

_INSERT = (
    "INSERT INTO todo_import_drafts(import_id, user_id, scope_kind, organization_id, "
    "draft_json, revision, state, result_json, created_at, updated_at) "
    "VALUES (?, 'user-1', ?, ?, '{}', 1, ?, ?, 't', 't')"
)


def test_a_store_holding_todos_gains_drafts_whose_scope_and_result_match(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    before = _migrations_before(migrations, "0140_todo_import_drafts.sql")
    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=before)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.execute(
            "INSERT INTO todo_items(todo_id, user_id, text, author, origin_kind, "
            "created_at) VALUES ('t-1', 'user-1', 'Send', 'user', 'written', 't')"
        )

    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=migrations)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("SELECT todo_id FROM todo_items").fetchall() == [
            ("t-1",)
        ]
        connection.execute(_INSERT, ("i-1", "organization", "org-a", "open", None))
        connection.execute(_INSERT, ("i-2", "all", None, "registered", "{}"))
        for row in [
            ("i-3", "all", "org-a", "open", None),
            ("i-4", "organization", None, "open", None),
            ("i-5", "no_organization", None, "registered", None),
            ("i-6", "all", None, "canceled", "{}"),
        ]:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(_INSERT, row)

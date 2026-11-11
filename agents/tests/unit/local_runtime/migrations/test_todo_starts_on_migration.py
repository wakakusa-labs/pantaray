from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)

from .support import _insert_user, _migrations_through, apply_migrations

PROGRESS_MIGRATION_NAME = "0136_todo_progress.sql"
STARTS_ON_MIGRATION_NAME = "0137_todo_starts_on.sql"
BUSY_TIMEOUT_MS = 1_000


def test_a_store_holding_weekly_todos_upgrades_with_each_starting_on_its_monday(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, PROGRESS_MIGRATION_NAME),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.executemany(
            """
            INSERT INTO todo_items(
                todo_id, user_id, text, author, origin_kind, week_start, created_at
            ) VALUES (?, 'user-1', ?, 'user', 'written', ?, '2026-11-01T00:00:00Z')
            """,
            [("t-1", "Send the quote", "2026-10-26"), ("t-2", "Book", "2026-11-09")],
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, STARTS_ON_MIGRATION_NAME),
    )

    with closing(sqlite3.connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT todo_id, text, starts_on FROM todo_items ORDER BY todo_id"
        ).fetchall()
        indexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'todo_items' AND name LIKE 'idx_todo_items_user_%'"
        ).fetchall()
    assert rows == [
        ("t-1", "Send the quote", "2026-10-26"),
        ("t-2", "Book", "2026-11-09"),
    ]
    assert sorted(indexes) == [
        ("idx_todo_items_user_deleted",),
        ("idx_todo_items_user_starts_on",),
    ]

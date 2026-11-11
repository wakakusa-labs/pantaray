from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)

from .support import (
    _insert_user,
    _migrations_before,
    _migrations_through,
    apply_migrations,
)

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


START_UNSET_MIGRATION_NAME = "0139_todo_start_unset.sql"


def test_unsetting_the_start_keeps_every_row_its_progress_and_the_change_counter(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_before(migrations, START_UNSET_MIGRATION_NAME),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.execute(
            """
            INSERT INTO todo_items(
                todo_id, user_id, client_todo_id, text, author, origin_kind,
                starts_on, due_date, due_repeat, checked_by, checked_at, created_at
            ) VALUES (
                't-1', 'user-1', 'c-1', 'Send the quote', 'user', 'written',
                '2026-11-09', '2026-11-15', 'weekly', 'user', '2026-11-10T00:00:00Z',
                '2026-11-01T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO todo_progress_records(
                record_id, user_id, todo_id, quantity, unit, note, author, recorded_at
            ) VALUES ('r-1', 'user-1', 't-1', 2, 'pages', 'Drafted', 'user',
                '2026-11-10T00:00:00Z')
            """
        )
        before = connection.execute("SELECT * FROM todo_items").fetchall()

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, START_UNSET_MIGRATION_NAME),
    )

    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("SELECT * FROM todo_items").fetchall() == before
        assert connection.execute(
            "SELECT record_id FROM todo_progress_records"
        ).fetchall() == [("r-1",)]
        revision = connection.execute(
            "SELECT revision FROM todo_revisions WHERE user_id = 'user-1'"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO todo_items(
                todo_id, user_id, text, author, origin_kind, starts_on, created_at
            ) VALUES ('t-2', 'user-1', 'Book', 'user', 'written', NULL,
                '2026-11-12T00:00:00Z')
            """
        )
        assert connection.execute(
            "SELECT revision FROM todo_revisions WHERE user_id = 'user-1'"
        ).fetchone() == (revision[0] + 1,)
        indexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'todo_items' AND name LIKE 'idx_%'"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert sorted(indexes) == [
        ("idx_todo_items_project",),
        ("idx_todo_items_user_deleted",),
        ("idx_todo_items_user_starts_on",),
    ]

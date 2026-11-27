from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)

from .support import (
    _insert_user,
    _migrations_before,
    _migrations_through,
    apply_migrations,
)

HOOK_ORIGIN_MIGRATION_NAME = "0156_todo_hook_origin.sql"
BUSY_TIMEOUT_MS = 1_000
_COLUMNS = """todo_id, user_id, project_id, organization_id, client_todo_id, text,
    author, origin_kind, suggestion_id, previous_todo_id, starts_on, due_date,
    due_repeat, checked_by, checked_at, report_kind, report_note, report_run_id,
    report_at, deletion_request_id, deletion_request_reason, deletion_requested_at,
    deletion_request_state, deleted_by, deleted_at, created_at, attachments_json"""


def test_the_hook_origin_rebuild_keeps_every_row_its_progress_and_counter(
    tmp_path: Path,
) -> None:
    # Catches a rebuild that drops a column's values, cascades the progress
    # records away, or loses an index or the change counter's triggers.
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_before(migrations, HOOK_ORIGIN_MIGRATION_NAME),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.executescript(
            """
            INSERT INTO workspace_organizations(organization_id, user_id,
              display_name, created_at, updated_at)
            VALUES ('org-1', 'user-1', 'Org', 'T', 'T');
            INSERT INTO todo_items(
                todo_id, user_id, organization_id, client_todo_id, text, author,
                origin_kind, starts_on, due_date, due_repeat, checked_by,
                checked_at, report_kind, report_note, report_at,
                deletion_request_id, deletion_request_reason,
                deletion_requested_at, deletion_request_state, created_at,
                attachments_json
            ) VALUES (
                't-1', 'user-1', 'org-1', 'c-1', 'Send the quote', 'user',
                'written', '2026-11-09', '2026-11-15', 'weekly', 'user',
                '2026-11-10T00:00:00Z', 'partial', 'Half', '2026-11-10T00:00:00Z',
                'd-1', 'Done', '2026-11-10T00:00:00Z', 'declined',
                '2026-11-01T00:00:00Z', '{"files": [], "images": []}'
            );
            INSERT INTO todo_items(
                todo_id, user_id, text, author, origin_kind, suggestion_id,
                deleted_by, deleted_at, created_at
            ) VALUES (
                't-2', 'user-1', 'Book', 'ai', 'suggestion', 's-1', 'user',
                '2026-11-11T00:00:00Z', '2026-11-01T00:00:00Z'
            );
            INSERT INTO todo_progress_records(
                record_id, user_id, todo_id, quantity, unit, note, author, recorded_at
            ) VALUES ('r-1', 'user-1', 't-1', 2, 'pages', 'Drafted', 'user',
                '2026-11-10T00:00:00Z');
            """
        )
        before = connection.execute(
            f"SELECT {_COLUMNS} FROM todo_items ORDER BY todo_id"
        ).fetchall()

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, HOOK_ORIGIN_MIGRATION_NAME),
    )

    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert (
            connection.execute(
                f"SELECT {_COLUMNS} FROM todo_items ORDER BY todo_id"
            ).fetchall()
            == before
        )
        assert connection.execute(
            "SELECT record_id FROM todo_progress_records"
        ).fetchall() == [("r-1",)]
        (revision,) = connection.execute(
            "SELECT revision FROM todo_revisions WHERE user_id = 'user-1'"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO todo_items(
                todo_id, user_id, text, author, origin_kind, hook_origin_action_id,
                created_at
            ) VALUES ('t-3', 'user-1', 'Build report', 'ai', 'hook', 'action-1',
                '2026-11-12T00:00:00Z')
            """
        )
        assert connection.execute(
            "SELECT revision FROM todo_revisions WHERE user_id = 'user-1'"
        ).fetchone() == (revision + 1,)
        # A hook's TODO names the conversation that asked, and only it does.
        for origin_kind, hook_origin_action_id in (("hook", None), ("written", "a")):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO todo_items(
                        todo_id, user_id, text, author, origin_kind,
                        hook_origin_action_id, created_at
                    ) VALUES ('t-4', 'user-1', 'X', 'ai', ?, ?, 'T')
                    """,
                    (origin_kind, hook_origin_action_id),
                )
        indexes = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'todo_items' AND name LIKE 'idx_%'"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert sorted(indexes) == [
        ("idx_todo_items_organization",),
        ("idx_todo_items_project",),
        ("idx_todo_items_user_deleted",),
        ("idx_todo_items_user_starts_on",),
    ]

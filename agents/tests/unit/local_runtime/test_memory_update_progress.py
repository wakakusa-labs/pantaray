from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    append_memory_category_published_in_connection,
    append_memory_update_completed_in_connection,
    load_published_memory_categories,
    memory_update_is_complete,
)
from pantaray_agents.local_runtime.runtime.session_store import import_desktop_session
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)

from .migrated_db import prepare_test_database

USER_ID = "user-1"
PROCESS_ID = "process-1"
NOW = "2026-09-07T00:00:00Z"


def _prepare_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER_ID,
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, started_at, updated_at,
                heartbeat_at, next_event_seq
            ) VALUES (?, ?, 'memory', 'running', ?, ?, ?, 1)
            """,
            (PROCESS_ID, USER_ID, NOW, NOW, NOW),
        )
    return db_path


def test_published_categories_survive_a_restart_of_the_same_run(
    tmp_path: Path,
) -> None:
    db_path = _prepare_db(tmp_path)
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=1_000
    ) as connection:
        with connection:
            append_memory_category_published_in_connection(
                connection=connection,
                process_id=PROCESS_ID,
                source="fact",
                revision_id="rev-1",
                created_at=NOW,
            )
        assert load_published_memory_categories(
            connection=connection, process_id=PROCESS_ID
        ) == frozenset({"fact"})
        assert not memory_update_is_complete(
            connection=connection, process_id=PROCESS_ID
        )
        with connection:
            append_memory_update_completed_in_connection(
                connection=connection,
                process_id=PROCESS_ID,
                published_sources=("fact",),
                created_at=NOW,
            )
        assert memory_update_is_complete(connection=connection, process_id=PROCESS_ID)

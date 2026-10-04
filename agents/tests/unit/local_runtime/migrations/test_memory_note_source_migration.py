from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemorySource
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    verify_database_integrity,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .support import _insert_user, _migrations_before

MEMORY_NOTE_MIGRATION_NAME = "0121_memory_note_source.sql"
USER_ID = "user-note"
BUSY_TIMEOUT_MS = 1_000


def _register(db_path: Path, source: MemorySource, record_id: str) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with immediate_transaction(connection):
            register_inline_domain_memory(
                connection=connection,
                user_id=USER_ID,
                source=source,
                source_record_id=record_id,
                content=f"{source} body",
            )


def _nodes(db_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT * FROM memory_nodes ORDER BY node_id"
        ).fetchall()


def test_upgrade_keeps_catalog_rows_and_accepts_memory_notes(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_before(migrations, MEMORY_NOTE_MIGRATION_NAME),
    )
    with sqlite3.connect(db_path) as connection:
        _insert_user(connection, USER_ID)
    for source, record_id in (("action_file_read", "inv-1"), ("source_records", "r")):
        _register(db_path, source, record_id)
    with pytest.raises(sqlite3.IntegrityError):
        _register(db_path, "memory_note", "act-1:step-1")
    before = _nodes(db_path)

    apply_migrations(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, migrations=migrations
    )
    verify_database_integrity(db_path, BUSY_TIMEOUT_MS)

    assert _nodes(db_path) == before
    _register(db_path, "memory_note", "act-1:step-1")
    assert len(_nodes(db_path)) == len(before) + 1

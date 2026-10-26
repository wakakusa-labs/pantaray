from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.embedding_generation_storage import (
    create_embedding_generation_storage,
)
from pantaray_agents.local_runtime.storage.migrations import (
    _configure_connection,
    apply_migrations,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)

from .support import _migrations_before

SEARCH_INDEXES_MIGRATION_NAME = "0132_memory_search_indexes.sql"
BUSY_TIMEOUT_MS = 1_000
TIMESTAMP = "2026-10-26T00:00:00Z"
ENTRY_INDEX_COLUMNS = [
    "user_id",
    "revision_id",
    "fragment_id",
    "fragment_content_sha256",
]


def test_search_index_migration_indexes_fragments_and_every_generation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_before(migrations, SEARCH_INDEXES_MIGRATION_NAME),
    )
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=BUSY_TIMEOUT_MS)
        # Generation 1 is the store's bootstrap generation, whose entry table
        # predates the index. Generation 2 is created by this build, as the
        # v84 rebase does during an upgrade, so its table already has it.
        with connection:
            connection.execute(
                """
                INSERT INTO memory_embedding_generations(
                    generation_id, profile_id, model_id, dimensions,
                    normalized, distance_metric, max_text_chars, created_at
                ) SELECT 2, profile_id, model_id, dimensions, normalized,
                         distance_metric, max_text_chars, ?
                  FROM memory_embedding_generations WHERE generation_id = 1
                """,
                (TIMESTAMP,),
            )
            create_embedding_generation_storage(
                connection, generation_id=2, dimensions=512
            )
        assert _index_columns(connection, "memory_embedding_entries_g1_revision") == []
        assert (
            _index_columns(connection, "memory_embedding_entries_g2_revision")
            == ENTRY_INDEX_COLUMNS
        )

    apply_migrations(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, migrations=migrations
    )

    with sqlite3.connect(db_path) as connection:
        assert _index_columns(connection, "idx_memory_fragments_source_path") == [
            "user_id",
            "source_path",
        ]
        for generation_id in (1, 2):
            assert (
                _index_columns(
                    connection, f"memory_embedding_entries_g{generation_id}_revision"
                )
                == ENTRY_INDEX_COLUMNS
            )


def _index_columns(connection: sqlite3.Connection, index: str) -> list[str]:
    return [
        str(row[2])
        for row in connection.execute(
            "SELECT * FROM pragma_index_info(?) ORDER BY seqno", (index,)
        ).fetchall()
    ]

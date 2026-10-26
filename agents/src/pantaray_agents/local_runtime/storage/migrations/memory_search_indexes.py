from __future__ import annotations

import sqlite3

from .sql_script import execute_sql_statements


def apply_memory_search_indexes_migration(
    connection: sqlite3.Connection, *, migration_statements: tuple[str, ...]
) -> None:
    """Adds the search indexes, including one per existing embedding generation.

    Each generation has its own entry table, so a static statement cannot name
    them; a generation created later gets the same index with its storage.
    """
    execute_sql_statements(connection, migration_statements)
    # Catalog imports depend on initialized migration modules.
    from pantaray_agents.local_runtime.memory_catalog.embedding_generation_storage import (
        create_generation_entry_revision_index,
    )

    generation_ids = connection.execute(
        "SELECT generation_id FROM memory_embedding_generations ORDER BY generation_id"
    ).fetchall()
    for (generation_id,) in generation_ids:
        create_generation_entry_revision_index(
            connection, generation_id=int(generation_id)
        )

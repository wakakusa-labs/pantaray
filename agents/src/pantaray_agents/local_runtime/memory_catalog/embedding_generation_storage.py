from __future__ import annotations

import sqlite3


def require_generation_id(generation_id: int) -> int:
    if isinstance(generation_id, bool) or not isinstance(generation_id, int):
        raise TypeError("generation_id must be an integer")
    if generation_id <= 0:
        raise ValueError("generation_id must be positive")
    return generation_id


def generation_entry_table(generation_id: int) -> str:
    return f"memory_embedding_entries_g{require_generation_id(generation_id)}"


def generation_vector_table(generation_id: int) -> str:
    return f"memory_embedding_vectors_g{require_generation_id(generation_id)}"


def create_embedding_generation_storage(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
    dimensions: int,
) -> None:
    entries = generation_entry_table(generation_id)
    vectors = generation_vector_table(generation_id)
    trigger = f"{entries}_ad"
    connection.execute(
        f"""
        CREATE TABLE {entries} (
            embedding_id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            generation_id INTEGER NOT NULL CHECK (generation_id = {generation_id}),
            fragment_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
            revision_id TEXT NOT NULL,
            fragment_content_sha256 TEXT NOT NULL
                CHECK (length(fragment_content_sha256) = 64),
            chunk_content_sha256 TEXT NOT NULL
                CHECK (length(chunk_content_sha256) = 64),
            created_at TEXT NOT NULL,
            UNIQUE (user_id, fragment_id, chunk_index),
            FOREIGN KEY (user_id, generation_id)
                REFERENCES memory_embedding_user_generations(user_id, generation_id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id, revision_id, fragment_id)
                REFERENCES memory_fragments(user_id, revision_id, fragment_id)
                ON DELETE CASCADE
        )
        """
    )
    create_generation_entry_revision_index(connection, generation_id=generation_id)
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE {vectors} USING vec0(
            embedding_id INTEGER PRIMARY KEY,
            embedding FLOAT[{dimensions}] distance_metric=cosine,
            user_id TEXT PARTITION KEY,
            revision_id TEXT
        )
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER {trigger}
        AFTER DELETE ON {entries}
        BEGIN
            DELETE FROM {vectors} WHERE embedding_id = old.embedding_id;
        END
        """
    )


def create_generation_entry_revision_index(
    connection: sqlite3.Connection, *, generation_id: int
) -> None:
    """Index the entries by revision, for semantic search and fragment deletes.

    Semantic search reads the visible revisions' entries from this index alone,
    so it carries every column that search reads. Its leading columns are the
    fragment foreign key, so deleting a fragment finds its entries without a
    scan of the table.
    """
    entries = generation_entry_table(generation_id)
    connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {entries}_revision
        ON {entries}(user_id, revision_id, fragment_id, fragment_content_sha256)
        """
    )


def drop_embedding_generation_storage(
    connection: sqlite3.Connection,
    *,
    generation_id: int,
) -> None:
    entries = generation_entry_table(generation_id)
    vectors = generation_vector_table(generation_id)
    connection.execute(f"DROP TRIGGER IF EXISTS {entries}_ad")
    connection.execute(f"DROP TABLE {vectors}")
    connection.execute(f"DROP TABLE {entries}")


__all__ = [
    "create_embedding_generation_storage",
    "create_generation_entry_revision_index",
    "drop_embedding_generation_storage",
    "generation_entry_table",
    "generation_vector_table",
    "require_generation_id",
]

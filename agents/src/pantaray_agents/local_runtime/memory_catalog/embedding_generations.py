from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.embedding_local import LocalEmbeddingManifest
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .connection import open_memory_catalog_connection
from .embedding_consistency import (
    MemoryEmbeddingConsistencyError,
    verify_embedding_generation_complete,
)
from .embedding_generation_storage import (
    create_embedding_generation_storage,
    drop_embedding_generation_storage,
    generation_entry_table,
    generation_vector_table,
    require_generation_id,
)

_DISTANCE_METRIC = "cosine"


class MemoryEmbeddingGenerationError(RuntimeError):
    """Raised when an embedding generation cannot be used safely."""


class MemoryEmbeddingActivationError(MemoryEmbeddingGenerationError):
    """Raised when an incomplete generation is activated."""


@dataclass(frozen=True, slots=True)
class EmbeddingSpecification:
    """What a generation's vectors mean.

    A generation is only reused while every field still matches, so a new model
    artifact -- which carries a new profile id -- is always indexed from scratch
    instead of mixing its vectors into an index built by the previous one. The
    fields are read back from generations older than the current model as well,
    so nothing here is checked against the model that is installed now.
    """

    profile_id: str
    model_id: str
    dimensions: int
    normalized: bool
    max_text_chars: int
    distance_metric: str = _DISTANCE_METRIC

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("embedding profile_id must not be empty")
        if not self.model_id.strip():
            raise ValueError("embedding model_id must not be empty")
        if isinstance(self.dimensions, bool) or self.dimensions <= 0:
            raise ValueError("embedding dimensions must be positive")
        if not isinstance(self.normalized, bool):
            raise TypeError("embedding normalized must be a boolean")
        if isinstance(self.max_text_chars, bool) or not isinstance(
            self.max_text_chars, int
        ):
            raise TypeError("embedding max_text_chars must be an integer")
        if self.max_text_chars <= 0:
            raise ValueError("embedding max_text_chars must be positive")
        if self.distance_metric != _DISTANCE_METRIC:
            raise ValueError("only cosine embedding distance is supported")


@dataclass(frozen=True, slots=True)
class EmbeddingGeneration:
    generation_id: int
    specification: EmbeddingSpecification


def current_embedding_specification(
    manifest: LocalEmbeddingManifest,
) -> EmbeddingSpecification:
    """The specification the installed model artifact indexes at."""
    return EmbeddingSpecification(
        profile_id=manifest.profile_id,
        model_id=manifest.model_id,
        dimensions=manifest.dimensions,
        # The local runner L2 normalizes every vector because the index ranks by
        # cosine distance; `search_semantic_fragments` refuses anything else.
        normalized=True,
        max_text_chars=manifest.max_text_chars,
    )


def ensure_user_embedding_generation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    specification: EmbeddingSpecification,
) -> EmbeddingGeneration:
    normalized_user_id = _require_non_empty(user_id, field_name="user_id")
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            _require_user(connection, normalized_user_id)
            scopes = _load_user_scopes(connection, normalized_user_id)
            active = next((item for state, item in scopes if state == "active"), None)
            building = next(
                (item for state, item in scopes if state == "building"), None
            )
            if building is not None:
                if building.specification == specification:
                    return building
                _delete_user_scope(
                    connection,
                    user_id=normalized_user_id,
                    generation_id=building.generation_id,
                )
            selected = _select_or_create_without_building(
                connection,
                user_id=normalized_user_id,
                active=active,
                specification=specification,
            )
            _drop_unused_generations(connection)
            return selected


def start_embedding_reprojection(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    specification: EmbeddingSpecification,
) -> EmbeddingGeneration:
    normalized_user_id = _require_non_empty(user_id, field_name="user_id")
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            _require_user(connection, normalized_user_id)
            building = connection.execute(
                """
                SELECT generation_id
                FROM memory_embedding_user_generations
                WHERE user_id = ? AND state = 'building'
                """,
                (normalized_user_id,),
            ).fetchone()
            if building is not None:
                _delete_user_scope(
                    connection,
                    user_id=normalized_user_id,
                    generation_id=int(building[0]),
                )
            generation = _create_generation(connection, specification)
            _create_user_build(
                connection,
                user_id=normalized_user_id,
                generation_id=generation.generation_id,
            )
            _drop_unused_generations(connection)
            return generation


def activate_embedding_generation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    generation_id: int,
    activated_at: str | None = None,
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            activate_embedding_generation_in_transaction(
                connection,
                user_id=user_id,
                generation_id=generation_id,
                activated_at=activated_at,
            )


def activate_embedding_generation_in_transaction(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    generation_id: int,
    activated_at: str | None = None,
) -> None:
    if not connection.in_transaction:
        raise MemoryEmbeddingActivationError(
            "embedding activation requires an owning transaction"
        )
    normalized_user_id = _require_non_empty(user_id, field_name="user_id")
    selected_generation_id = require_generation_id(generation_id)
    timestamp = _require_non_empty(
        activated_at or now_utc_iso(), field_name="activated_at"
    )
    state = connection.execute(
        """
        SELECT state FROM memory_embedding_user_generations
        WHERE user_id = ? AND generation_id = ?
        """,
        (normalized_user_id, selected_generation_id),
    ).fetchone()
    if state is None or str(state[0]) != "building":
        raise MemoryEmbeddingActivationError(
            "embedding generation is not the user's building generation"
        )
    try:
        verify_embedding_generation_complete(
            connection,
            user_id=normalized_user_id,
            generation_id=selected_generation_id,
            entries=generation_entry_table(selected_generation_id),
            vectors=generation_vector_table(selected_generation_id),
        )
    except MemoryEmbeddingConsistencyError as exc:
        raise MemoryEmbeddingActivationError(str(exc)) from exc
    active = connection.execute(
        """
        SELECT generation_id FROM memory_embedding_user_generations
        WHERE user_id = ? AND state = 'active'
        """,
        (normalized_user_id,),
    ).fetchone()
    if active is not None:
        _delete_user_scope(
            connection,
            user_id=normalized_user_id,
            generation_id=int(active[0]),
        )
    updated = connection.execute(
        """
        UPDATE memory_embedding_user_generations
        SET state = 'active', updated_at = ?, activated_at = ?
        WHERE user_id = ? AND generation_id = ? AND state = 'building'
        """,
        (timestamp, timestamp, normalized_user_id, selected_generation_id),
    ).rowcount
    if updated != 1:
        raise MemoryEmbeddingActivationError(
            "embedding generation activation lost its target scope"
        )
    _drop_unused_generations(connection)


def load_active_embedding_generation(
    connection: sqlite3.Connection, *, user_id: str
) -> EmbeddingGeneration | None:
    row = connection.execute(
        """
        SELECT generations.generation_id,
               generations.profile_id,
               generations.model_id,
               generations.dimensions,
               generations.normalized,
               generations.distance_metric,
               generations.max_text_chars
        FROM memory_embedding_user_generations AS scopes
        JOIN memory_embedding_generations AS generations
          ON generations.generation_id = scopes.generation_id
        WHERE scopes.user_id = ? AND scopes.state = 'active'
        """,
        (_require_non_empty(user_id, field_name="user_id"),),
    ).fetchone()
    return _generation_from_row(row) if row is not None else None


def delete_user_embedding_generations_in_transaction(
    connection: sqlite3.Connection, *, user_id: str
) -> None:
    if not connection.in_transaction:
        raise MemoryEmbeddingGenerationError(
            "embedding generation deletion requires an owning transaction"
        )
    connection.execute(
        "DELETE FROM memory_embedding_user_generations WHERE user_id = ?",
        (_require_non_empty(user_id, field_name="user_id"),),
    )
    _drop_unused_generations(connection)


def _load_user_scopes(
    connection: sqlite3.Connection, user_id: str
) -> tuple[tuple[str, EmbeddingGeneration], ...]:
    rows = connection.execute(
        """
        SELECT scopes.state,
               generations.generation_id,
               generations.profile_id,
               generations.model_id,
               generations.dimensions,
               generations.normalized,
               generations.distance_metric,
               generations.max_text_chars
        FROM memory_embedding_user_generations AS scopes
        JOIN memory_embedding_generations AS generations
          ON generations.generation_id = scopes.generation_id
        WHERE scopes.user_id = ?
        ORDER BY scopes.state, generations.generation_id
        """,
        (user_id,),
    ).fetchall()
    return tuple((str(row[0]), _generation_from_row(row, offset=1)) for row in rows)


def _generation_from_row(
    row: sqlite3.Row | tuple[object, ...], *, offset: int = 0
) -> EmbeddingGeneration:
    return EmbeddingGeneration(
        generation_id=require_generation_id(_db_integer(row[offset])),
        specification=EmbeddingSpecification(
            profile_id=str(row[offset + 1]),
            model_id=str(row[offset + 2]),
            dimensions=_db_integer(row[offset + 3]),
            normalized=bool(row[offset + 4]),
            distance_metric=str(row[offset + 5]),
            max_text_chars=_db_integer(row[offset + 6]),
        ),
    )


def _reuse_or_create_generation(
    connection: sqlite3.Connection, specification: EmbeddingSpecification
) -> EmbeddingGeneration:
    row = connection.execute(
        """
        SELECT generation_id, profile_id, model_id, dimensions,
               normalized, distance_metric, max_text_chars
        FROM memory_embedding_generations
        WHERE profile_id = ? AND model_id = ? AND dimensions = ?
          AND normalized = ? AND distance_metric = ? AND max_text_chars = ?
        ORDER BY generation_id DESC
        LIMIT 1
        """,
        (
            specification.profile_id,
            specification.model_id,
            specification.dimensions,
            int(specification.normalized),
            specification.distance_metric,
            specification.max_text_chars,
        ),
    ).fetchone()
    return (
        _generation_from_row(row)
        if row is not None
        else _create_generation(connection, specification)
    )


def _select_or_create_without_building(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    active: EmbeddingGeneration | None,
    specification: EmbeddingSpecification,
) -> EmbeddingGeneration:
    if active is not None and active.specification == specification:
        return active
    building = _reuse_or_create_generation(connection, specification)
    _create_user_build(
        connection,
        user_id=user_id,
        generation_id=building.generation_id,
    )
    return building


def _create_generation(
    connection: sqlite3.Connection, specification: EmbeddingSpecification
) -> EmbeddingGeneration:
    generation_id = int(
        connection.execute(
            "SELECT COALESCE(MAX(generation_id), 0) + 1 FROM memory_embedding_generations"
        ).fetchone()[0]
    )
    created_at = now_utc_iso()
    connection.execute(
        """
        INSERT INTO memory_embedding_generations(
            generation_id, profile_id, model_id, dimensions, normalized,
            distance_metric, max_text_chars, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generation_id,
            specification.profile_id,
            specification.model_id,
            specification.dimensions,
            int(specification.normalized),
            specification.distance_metric,
            specification.max_text_chars,
            created_at,
        ),
    )
    create_embedding_generation_storage(
        connection,
        generation_id=generation_id,
        dimensions=specification.dimensions,
    )
    return EmbeddingGeneration(generation_id, specification)


def _create_user_build(
    connection: sqlite3.Connection, *, user_id: str, generation_id: int
) -> None:
    timestamp = now_utc_iso()
    connection.execute(
        """
        INSERT INTO memory_embedding_user_generations(
            user_id, generation_id, state, created_at, updated_at, activated_at
        ) VALUES (?, ?, 'building', ?, ?, NULL)
        """,
        (user_id, generation_id, timestamp, timestamp),
    )
    connection.execute(
        """
        WITH RECURSIVE fragment_chunks(
            user_id, fragment_id, chunk_index, content_length, max_text_chars
        ) AS (
            SELECT fragments.user_id, fragments.fragment_id, 0,
                   length(fragments.content_text), generations.max_text_chars
            FROM memory_fragments AS fragments
            JOIN memory_embedding_generations AS generations
              ON generations.generation_id = ?
            WHERE fragments.user_id = ?
              AND fragments.block_kind NOT IN ('record_root', 'document_root')
              AND length(fragments.content_text) > 0
            UNION ALL
            SELECT user_id, fragment_id, chunk_index + 1,
                   content_length, max_text_chars
            FROM fragment_chunks
            WHERE (chunk_index + 1) * max_text_chars < content_length
        )
        INSERT INTO memory_embedding_work(
            user_id, generation_id, fragment_id, chunk_index,
            state, attempt_count,
            next_attempt_at, last_error_code, created_at, updated_at
        )
        SELECT user_id, ?, fragment_id, chunk_index,
               'pending', 0, NULL, NULL, ?, ?
        FROM fragment_chunks
        """,
        (generation_id, user_id, generation_id, timestamp, timestamp),
    )


def _delete_user_scope(
    connection: sqlite3.Connection, *, user_id: str, generation_id: int
) -> None:
    connection.execute(
        """
        DELETE FROM memory_embedding_user_generations
        WHERE user_id = ? AND generation_id = ?
        """,
        (user_id, require_generation_id(generation_id)),
    )


def _drop_unused_generations(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT generations.generation_id
        FROM memory_embedding_generations AS generations
        LEFT JOIN memory_embedding_user_generations AS scopes
          ON scopes.generation_id = generations.generation_id
        WHERE scopes.generation_id IS NULL
        ORDER BY generations.generation_id
        """
    ).fetchall()
    for row in rows:
        generation_id = require_generation_id(int(row[0]))
        drop_embedding_generation_storage(connection, generation_id=generation_id)
        connection.execute(
            "DELETE FROM memory_embedding_generations WHERE generation_id = ?",
            (generation_id,),
        )


def _require_user(connection: sqlite3.Connection, user_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        is None
    ):
        raise MemoryEmbeddingGenerationError("embedding user does not exist")


def _db_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryEmbeddingGenerationError("embedding generation row is invalid")
    return value


def _require_non_empty(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


__all__ = [
    "EmbeddingGeneration",
    "EmbeddingSpecification",
    "MemoryEmbeddingActivationError",
    "MemoryEmbeddingGenerationError",
    "activate_embedding_generation",
    "activate_embedding_generation_in_transaction",
    "current_embedding_specification",
    "delete_user_embedding_generations_in_transaction",
    "ensure_user_embedding_generation",
    "generation_entry_table",
    "generation_vector_table",
    "load_active_embedding_generation",
    "start_embedding_reprojection",
]

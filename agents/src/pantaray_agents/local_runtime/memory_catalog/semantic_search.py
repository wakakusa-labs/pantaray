from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)

from .embedding_chunks import (
    memory_embedding_chunk_count_from_length,
    validate_memory_embedding_chunk,
)
from .embedding_consistency import UNEMBEDDABLE_CHUNK_SQL
from .embedding_generations import (
    EmbeddingGeneration,
    generation_entry_table,
    generation_vector_table,
)
from .semantic_index import (
    MemoryEmbeddingIndexUnavailableError,
    MemoryEmbeddingValidationError,
    encode_embedding,
)

_SQLITE_REVISION_ID_CHUNK_SIZE = 500
# sqlite-vec limits each KNN query to k <= 4096.
_SQLITE_VEC_MAX_K = 4_096


@dataclass(frozen=True, slots=True)
class SemanticFragmentMatch:
    fragment_id: str
    content_sha256: str
    distance: float


@dataclass(frozen=True, slots=True)
class _ProjectedEmbedding:
    embedding_id: int
    fragment_id: str
    content_sha256: str
    revision_id: str


def search_semantic_fragments(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    visible_revision_ids: Sequence[str],
    query_embedding: Sequence[float],
    embedding_generation: EmbeddingGeneration,
    candidate_limit: int,
) -> tuple[SemanticFragmentMatch, ...]:
    if candidate_limit <= 0:
        raise ValueError("semantic candidate_limit must be positive")
    revision_ids = tuple(dict.fromkeys(visible_revision_ids))
    if not revision_ids:
        return ()
    if not embedding_generation.specification.normalized:
        raise MemoryEmbeddingIndexUnavailableError(
            "active memory embedding generation is not normalized"
        )
    try:
        query_blob = encode_embedding(
            query_embedding,
            dimensions=embedding_generation.specification.dimensions,
        )
    except MemoryEmbeddingValidationError as exc:
        raise MemoryEmbeddingIndexUnavailableError(
            "memory search query embedding is invalid"
        ) from exc
    entries = generation_entry_table(embedding_generation.generation_id)
    vectors = generation_vector_table(embedding_generation.generation_id)
    best_by_fragment: dict[str, SemanticFragmentMatch] = {}
    try:
        for offset in range(0, len(revision_ids), _SQLITE_REVISION_ID_CHUNK_SIZE):
            revision_chunk = revision_ids[
                offset : offset + _SQLITE_REVISION_ID_CHUNK_SIZE
            ]
            projected = _verify_projected_chunk(
                connection,
                entries=entries,
                user_id=user_id,
                generation_id=embedding_generation.generation_id,
                revision_ids=revision_chunk,
                max_text_chars=embedding_generation.specification.max_text_chars,
            )
            for start in range(0, len(projected), _SQLITE_VEC_MAX_K):
                # Retrieve every distance before deduplication and limiting so
                # later chunks and equal-distance fragments cannot be dropped.
                matches = _search_embedding_batch(
                    connection,
                    vectors=vectors,
                    user_id=user_id,
                    projected=projected[start : start + _SQLITE_VEC_MAX_K],
                    query_blob=query_blob,
                )
                for match in matches:
                    current = best_by_fragment.get(match.fragment_id)
                    if current is None or match.distance < current.distance:
                        best_by_fragment[match.fragment_id] = match
    except sqlite3.OperationalError as exc:
        if is_retryable_repository_exception(exc):
            raise
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index schema is unavailable"
        ) from exc
    ranked = sorted(
        best_by_fragment.values(),
        key=lambda match: (match.distance, match.fragment_id),
    )
    return tuple(ranked[:candidate_limit])


def _verify_projected_chunk(
    connection: sqlite3.Connection,
    *,
    entries: str,
    user_id: str,
    generation_id: int,
    revision_ids: Sequence[str],
    max_text_chars: int,
) -> tuple[_ProjectedEmbedding, ...]:
    placeholders = ", ".join("?" for _ in revision_ids)
    parameters = (user_id, *revision_ids)
    entry_rows = connection.execute(
        f"""
        SELECT entries.fragment_id, entries.chunk_index,
               entries.fragment_content_sha256,
               entries.chunk_content_sha256,
               fragments.content_text, fragments.content_sha256,
               entries.embedding_id, entries.revision_id
        FROM {entries} AS entries
        LEFT JOIN memory_fragments AS fragments
          ON fragments.user_id = entries.user_id
         AND fragments.fragment_id = entries.fragment_id
         AND fragments.revision_id = entries.revision_id
         AND fragments.content_sha256 = entries.fragment_content_sha256
         AND fragments.block_kind NOT IN ('record_root', 'document_root')
        WHERE entries.user_id = ?
          AND entries.revision_id IN ({placeholders})
        ORDER BY entries.embedding_id
        """,
        parameters,
    ).fetchall()
    for row in entry_rows:
        if row[4] is None or row[5] is None:
            raise MemoryEmbeddingIndexUnavailableError(
                "memory embedding index has stale projected rows"
            )
        try:
            validate_memory_embedding_chunk(
                fragment_content_text=str(row[4]),
                fragment_content_sha256=str(row[5]),
                chunk_index=int(row[1]),
                chunk_content_sha256=str(row[3]),
                max_text_chars=max_text_chars,
            )
        except ValueError as exc:
            raise MemoryEmbeddingIndexUnavailableError(
                "memory embedding index has invalid chunk projection"
            ) from exc
    entry_keys = {(str(row[0]), int(row[1])) for row in entry_rows}
    expected_keys = _expected_visible_chunk_keys(
        connection,
        user_id=user_id,
        revision_ids=revision_ids,
        max_text_chars=max_text_chars,
    )
    work_rows = connection.execute(
        f"""
        SELECT work.fragment_id, work.chunk_index,
               CASE WHEN {UNEMBEDDABLE_CHUNK_SQL} THEN 1 ELSE 0 END
        FROM memory_embedding_work AS work
        JOIN memory_fragments AS fragments
          ON fragments.user_id = work.user_id
         AND fragments.fragment_id = work.fragment_id
        WHERE work.user_id = ? AND work.generation_id = ?
          AND fragments.revision_id IN ({placeholders})
          AND fragments.block_kind NOT IN ('record_root', 'document_root')
        """,
        (user_id, generation_id, *revision_ids),
    ).fetchall()
    work_keys = {(str(row[0]), int(row[1])) for row in work_rows if not row[2]}
    # A chunk this model cannot embed is neither projected nor still owed, so it
    # drops out of what the index is expected to cover.
    unembeddable_keys = {(str(row[0]), int(row[1])) for row in work_rows if row[2]}
    if (
        entry_keys & (work_keys | unembeddable_keys)
        or expected_keys != entry_keys | work_keys | unembeddable_keys
    ):
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index has missing or duplicate chunk projections"
        )
    return tuple(
        _ProjectedEmbedding(
            embedding_id=int(row[6]),
            fragment_id=str(row[0]),
            content_sha256=str(row[2]),
            revision_id=str(row[7]),
        )
        for row in entry_rows
    )


def _expected_visible_chunk_keys(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    revision_ids: Sequence[str],
    max_text_chars: int,
) -> set[tuple[str, int]]:
    placeholders = ", ".join("?" for _ in revision_ids)
    rows = connection.execute(
        f"""
        SELECT fragment_id, length(content_text)
        FROM memory_fragments
        WHERE user_id = ? AND revision_id IN ({placeholders})
          AND block_kind NOT IN ('record_root', 'document_root')
        """,
        (user_id, *revision_ids),
    ).fetchall()
    expected: set[tuple[str, int]] = set()
    for row in rows:
        fragment_id = str(row[0])
        chunk_count = memory_embedding_chunk_count_from_length(
            int(row[1]),
            max_text_chars=max_text_chars,
        )
        expected.update(
            (fragment_id, chunk_index) for chunk_index in range(chunk_count)
        )
    return expected


def _search_embedding_batch(
    connection: sqlite3.Connection,
    *,
    vectors: str,
    user_id: str,
    projected: Sequence[_ProjectedEmbedding],
    query_blob: bytes,
) -> tuple[SemanticFragmentMatch, ...]:
    fragments = {entry.embedding_id: entry for entry in projected}
    # Keep a subquery even for one ID: SQLite folds IN (?) into equality,
    # which sqlite-vec applies after KNN limiting instead of before it.
    vector_rows = connection.execute(
        f"""
        SELECT embedding_id, distance, revision_id
        FROM {vectors}
        WHERE embedding MATCH ?
          AND k = ?
          AND user_id = ?
          AND embedding_id IN (SELECT value FROM json_each(?))
        ORDER BY distance
        """,
        (
            sqlite3.Binary(query_blob),
            len(fragments),
            user_id,
            json.dumps(list(fragments)),
        ),
    ).fetchall()
    # k is every projected entry, so each comes back once with the revision it
    # was projected from, or its vector is missing or stale. Counting vectors
    # by revision instead reads every row of the vec0 table, which filters a
    # metadata column row by row: about a second per call at 90k vectors.
    if len(vector_rows) != len(fragments) or any(
        fragments[int(row[0])].revision_id != str(row[2]) for row in vector_rows
    ):
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index has missing or stale projected vectors"
        )
    return tuple(
        SemanticFragmentMatch(
            fragment_id=fragments[int(row[0])].fragment_id,
            content_sha256=fragments[int(row[0])].content_sha256,
            distance=float(row[1]),
        )
        for row in vector_rows
    )


__all__ = ["SemanticFragmentMatch", "search_semantic_fragments"]

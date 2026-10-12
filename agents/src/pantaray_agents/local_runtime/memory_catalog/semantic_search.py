from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from pantaray_agents.schema.repository_errors import (
    is_retryable_repository_exception,
)

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
    query_embeddings: Sequence[Sequence[float]],
    embedding_generation: EmbeddingGeneration,
    candidate_limit: int,
) -> tuple[tuple[SemanticFragmentMatch, ...], ...]:
    """One ranked match list per query embedding, in the order given."""
    if candidate_limit <= 0:
        raise ValueError("semantic candidate_limit must be positive")
    if not embedding_generation.specification.normalized:
        raise MemoryEmbeddingIndexUnavailableError(
            "active memory embedding generation is not normalized"
        )
    try:
        query_blobs = tuple(
            encode_embedding(
                embedding,
                dimensions=embedding_generation.specification.dimensions,
            )
            for embedding in query_embeddings
        )
    except MemoryEmbeddingValidationError as exc:
        raise MemoryEmbeddingIndexUnavailableError(
            "memory search query embedding is invalid"
        ) from exc
    revision_ids = tuple(dict.fromkeys(visible_revision_ids))
    if not revision_ids or not query_blobs:
        return tuple(() for _ in query_blobs)
    entries = generation_entry_table(embedding_generation.generation_id)
    vectors = generation_vector_table(embedding_generation.generation_id)
    try:
        projected = _projected_entries(
            connection,
            entries=entries,
            user_id=user_id,
            revision_ids=revision_ids,
        )
        batches = tuple(
            projected[start : start + _SQLITE_VEC_MAX_K]
            for start in range(0, len(projected), _SQLITE_VEC_MAX_K)
        )

        return tuple(
            _rank_matches(
                connection,
                vectors=vectors,
                user_id=user_id,
                batches=batches,
                query_blob=query_blob,
                candidate_limit=candidate_limit,
            )
            for query_blob in query_blobs
        )
    except sqlite3.OperationalError as exc:
        if is_retryable_repository_exception(exc):
            raise
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index schema is unavailable"
        ) from exc


def _rank_matches(
    connection: sqlite3.Connection,
    *,
    vectors: str,
    user_id: str,
    batches: Sequence[Sequence[_ProjectedEmbedding]],
    query_blob: bytes,
    candidate_limit: int,
) -> tuple[SemanticFragmentMatch, ...]:
    best_by_fragment: dict[str, SemanticFragmentMatch] = {}
    for batch in batches:
        # Retrieve every distance before deduplication and limiting so later
        # chunks and equal-distance fragments cannot be dropped.
        for match in _search_embedding_batch(
            connection,
            vectors=vectors,
            user_id=user_id,
            projected=batch,
            query_blob=query_blob,
        ):
            current = best_by_fragment.get(match.fragment_id)
            if current is None or match.distance < current.distance:
                best_by_fragment[match.fragment_id] = match
    ranked = sorted(
        best_by_fragment.values(),
        key=lambda match: (match.distance, match.fragment_id),
    )
    return tuple(ranked[:candidate_limit])


def _projected_entries(
    connection: sqlite3.Connection,
    *,
    entries: str,
    user_id: str,
    revision_ids: Sequence[str],
) -> tuple[_ProjectedEmbedding, ...]:
    # Read without re-checking against fragment text: the writer validates each
    # chunk before storing it, fragment text never changes in place, deleting a
    # fragment cascades to its entries, and activation checks the whole
    # generation once. Re-checking here cost more than the nearest-neighbour
    # pass; the caller still matches each returned candidate's hash.
    #
    # NOT INDEXED: one rowid-order scan beats fetching each row through the
    # (user_id, ...) index and needs no sort for ORDER BY embedding_id.
    # Design limit: it reads every entry, visible or not (about 70 ms at 150k
    # entries); index revision_id when this read passes 300 ms.
    rows = connection.execute(
        f"""
        SELECT embedding_id, fragment_id, fragment_content_sha256, revision_id
        FROM {entries} NOT INDEXED
        WHERE user_id = ?
          AND revision_id IN (SELECT value FROM json_each(?))
        ORDER BY embedding_id
        """,
        (user_id, json.dumps(revision_ids)),
    ).fetchall()
    return tuple(
        _ProjectedEmbedding(
            embedding_id=int(row[0]),
            fragment_id=str(row[1]),
            content_sha256=str(row[2]),
            revision_id=str(row[3]),
        )
        for row in rows
    )


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

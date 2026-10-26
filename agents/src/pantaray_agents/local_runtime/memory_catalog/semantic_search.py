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
        if not projected:
            return tuple(() for _ in query_blobs)
        scored = _score_projected_vectors(
            connection,
            vectors=vectors,
            projected=projected,
            query_blobs=query_blobs,
        )
    except sqlite3.OperationalError as exc:
        if is_retryable_repository_exception(exc):
            raise
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index schema is unavailable"
        ) from exc
    return tuple(
        _rank_matches(scored, lane=lane, candidate_limit=candidate_limit)
        for lane in range(len(query_blobs))
    )


def _rank_matches(
    scored: Sequence[tuple[_ProjectedEmbedding, tuple[float, ...]]],
    *,
    lane: int,
    candidate_limit: int,
) -> tuple[SemanticFragmentMatch, ...]:
    # Sorting every entry by (distance, fragment_id) makes each fragment's
    # first appearance its best chunk, already in final rank order.
    ranked: list[SemanticFragmentMatch] = []
    seen: set[str] = set()
    for entry, distances in sorted(
        scored, key=lambda item: (item[1][lane], item[0].fragment_id)
    ):
        if entry.fragment_id in seen:
            continue
        seen.add(entry.fragment_id)
        ranked.append(
            SemanticFragmentMatch(
                fragment_id=entry.fragment_id,
                content_sha256=entry.content_sha256,
                distance=distances[lane],
            )
        )
        if len(ranked) == candidate_limit:
            break
    return tuple(ranked)


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
    # generation once. Re-checking here would hash every visible fragment's
    # text on each search; the caller still matches each returned candidate's
    # hash.
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


def _score_projected_vectors(
    connection: sqlite3.Connection,
    *,
    vectors: str,
    projected: Sequence[_ProjectedEmbedding],
    query_blobs: Sequence[bytes],
) -> list[tuple[_ProjectedEmbedding, tuple[float, ...]]]:
    """Each projected entry with its distance to every query, in query order.

    Scoring the visible vectors by id replaces a vec0 KNN query, which scans
    the user's whole partition, older revisions included, once per query.
    vec_distance_cosine is the function vec0 ranks with, so distances and
    near-ties match vec0 exactly; a dimension mismatch raises
    sqlite3.OperationalError.
    """
    # Design limit: this scores every visible vector (about 4 s at 53k
    # 384-dim vectors and two queries, about 3 s of it vec0 reading
    # revision_id row by row); move to an approximate nearest-neighbour index
    # when this read passes 10 s.
    by_id = {entry.embedding_id: entry for entry in projected}
    distance_columns = ", ".join(
        "vec_distance_cosine(embedding, ?)" for _ in query_blobs
    )
    rows = connection.execute(
        f"""
        SELECT embedding_id, revision_id, {distance_columns}
        FROM {vectors}
        WHERE embedding_id IN (SELECT value FROM json_each(?))
        """,
        (*query_blobs, json.dumps(list(by_id))),
    ).fetchall()
    # embedding_id is the vec0 primary key, so each projected entry comes back
    # once with the revision it was projected from, or its vector is missing
    # or stale.
    if len(rows) != len(projected) or any(
        by_id[int(row[0])].revision_id != str(row[1]) for row in rows
    ):
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index has missing or stale projected vectors"
        )
    return [
        (by_id[int(row[0])], tuple(float(value) for value in row[2:])) for row in rows
    ]


__all__ = ["SemanticFragmentMatch", "search_semantic_fragments"]

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

import numpy
from numpy.typing import NDArray

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

# Little-endian float32, the layout sqlite-vec stores FLOAT[n] columns in.
_FLOAT32 = numpy.dtype("<f4")


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
    dimensions = embedding_generation.specification.dimensions
    if not embedding_generation.specification.normalized:
        raise MemoryEmbeddingIndexUnavailableError(
            "active memory embedding generation is not normalized"
        )
    try:
        query_blobs = tuple(
            encode_embedding(embedding, dimensions=dimensions)
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
        matrix = _projected_vectors(
            connection,
            vectors=vectors,
            projected=projected,
            dimensions=dimensions,
        )
    except sqlite3.OperationalError as exc:
        if is_retryable_repository_exception(exc):
            raise
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index schema is unavailable"
        ) from exc
    queries = numpy.stack(
        [numpy.frombuffer(blob, dtype=_FLOAT32) for blob in query_blobs]
    )
    distances = _cosine_distances(matrix, queries)
    _, fragment_order = numpy.unique(
        [entry.fragment_id for entry in projected], return_inverse=True
    )
    return tuple(
        _rank_matches(
            projected,
            distances=distances[:, column],
            fragment_order=fragment_order,
            candidate_limit=candidate_limit,
        )
        for column in range(len(query_blobs))
    )


def _cosine_distances(
    matrix: NDArray[numpy.float32], queries: NDArray[numpy.float32]
) -> NDArray[numpy.float32]:
    """sqlite-vec's cosine distance, 1 - cosine similarity, in float32."""
    similarity = (matrix @ queries.T) / numpy.outer(_norms(matrix), _norms(queries))
    distances: NDArray[numpy.float32] = 1.0 - similarity
    return distances


def _norms(rows: NDArray[numpy.float32]) -> NDArray[numpy.float32]:
    # einsum avoids the full-size temporary numpy.linalg.norm squares into.
    norms: NDArray[numpy.float32] = numpy.sqrt(numpy.einsum("ij,ij->i", rows, rows))
    return norms


def _rank_matches(
    projected: Sequence[_ProjectedEmbedding],
    *,
    distances: NDArray[numpy.float32],
    fragment_order: NDArray[numpy.intp],
    candidate_limit: int,
) -> tuple[SemanticFragmentMatch, ...]:
    # Sorting every entry by (distance, fragment_id) makes each fragment's
    # first appearance its best chunk, already in final rank order.
    ranked: list[SemanticFragmentMatch] = []
    seen: set[str] = set()
    for index in numpy.lexsort((fragment_order, distances)):
        entry = projected[index]
        if entry.fragment_id in seen:
            continue
        seen.add(entry.fragment_id)
        ranked.append(
            SemanticFragmentMatch(
                fragment_id=entry.fragment_id,
                content_sha256=entry.content_sha256,
                distance=float(distances[index]),
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


def _projected_vectors(
    connection: sqlite3.Connection,
    *,
    vectors: str,
    projected: Sequence[_ProjectedEmbedding],
    dimensions: int,
) -> NDArray[numpy.float32]:
    """Each projected entry's vector, row-aligned with `projected`.

    Scoring the visible vectors here replaces a vec0 KNN query, which scans the
    user's whole partition, older revisions included, once per query.
    """
    # Design limit: this reads and holds every visible vector (about 4 s and
    # 80 MB at 53k 384-dim vectors, three quarters of it vec0 reading
    # revision_id row by row); move to an approximate nearest-neighbour index
    # when this read passes 10 s.
    rows_by_id = {entry.embedding_id: row for row, entry in enumerate(projected)}
    matrix = numpy.empty((len(projected), dimensions), dtype=_FLOAT32)
    filled = 0
    for embedding_id, blob, revision_id in connection.execute(
        f"""
        SELECT embedding_id, embedding, revision_id
        FROM {vectors}
        WHERE embedding_id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(list(rows_by_id)),),
    ):
        row = rows_by_id[int(embedding_id)]
        if (
            projected[row].revision_id != str(revision_id)
            or len(blob) != dimensions * _FLOAT32.itemsize
        ):
            raise MemoryEmbeddingIndexUnavailableError(
                "memory embedding index has missing or stale projected vectors"
            )
        matrix[row] = numpy.frombuffer(blob, dtype=_FLOAT32)
        filled += 1
    # Each embedding_id is the vec0 primary key, so one row per present vector.
    if filled != len(projected):
        raise MemoryEmbeddingIndexUnavailableError(
            "memory embedding index has missing or stale projected vectors"
        )
    return matrix


__all__ = ["SemanticFragmentMatch", "search_semantic_fragments"]

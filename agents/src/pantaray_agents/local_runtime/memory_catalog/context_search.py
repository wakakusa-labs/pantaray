from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from pantaray_agents.utils.timestamps import parse_iso8601_utc

from .context_ranking import (
    MemoryCandidateHit,
    RankedMemoryCandidate,
    rank_keyword_hits,
    rank_query_hits,
    select_memory_candidates,
)
from .embedding_generations import EmbeddingGeneration
from .epoch import build_memory_context_epoch, merge_memory_context_epochs
from .fragment_visibility import (
    MEMORY_FRAGMENT_COLUMNS,
    VISIBLE_FRAGMENT_JOINS,
    revision_visibility_predicate,
    visible_fragment_predicate,
)
from .lexical_search import search_lexical_fragments
from .models import (
    MemoryBlockKind,
    MemoryContextEpoch,
    MemoryFragment,
    MemoryNode,
    MemorySearchResult,
    MemorySource,
)
from .search_policy import (
    MEMORY_CATALOG_TO_SEARCH_SOURCE,
    MEMORY_SEARCH_FOCUS_SOURCES,
    MEMORY_SEARCH_TO_CATALOG_SOURCE,
    MemorySearchFocus,
)
from .semantic_index import (
    MemoryEmbeddingIndexUnavailableError,
)
from .semantic_search import SemanticFragmentMatch, search_semantic_fragments

LEXICAL_CANDIDATE_POOL_MULTIPLIER = 3


@dataclass(frozen=True, slots=True)
class SemanticSearchInput:
    """The embeddings of one search, all made by the generation's model."""

    generation: EmbeddingGeneration
    keywords_embedding: Sequence[float] | None
    query_embedding: Sequence[float] | None


def search_memory_catalog(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    run_id: str,
    keywords: tuple[str, ...],
    semantic: SemanticSearchInput | None,
    focus: MemorySearchFocus,
    center_time: str | None,
    radius_hours: int | None,
    limit: int,
    pinned_revisions: Mapping[MemorySource, str | None] | None = None,
) -> tuple[list[MemorySearchResult], MemoryContextEpoch]:
    if limit <= 0:
        raise ValueError("memory search limit must be positive")
    sources = _sources_for_focus(focus)
    if (center_time is None) != (radius_hours is None):
        raise ValueError("memory search time hint must be complete")
    if radius_hours is not None and radius_hours <= 0:
        raise ValueError("memory search time hint radius must be positive")
    parsed_center_time = (
        parse_iso8601_utc(center_time) if center_time is not None else None
    )

    candidate_limit = limit * LEXICAL_CANDIDATE_POOL_MULTIPLIER
    exact_hits = (
        _exact_hits(
            connection=connection,
            user_id=user_id,
            keywords=keywords,
            sources=sources,
            pinned_revisions=pinned_revisions,
            candidate_limit=candidate_limit,
        )
        if keywords
        else ()
    )
    # A fragment ID names one fragment; it is not a word to match elsewhere.
    fragment_ids = {hit.fragment.fragment_id for hit in exact_hits}
    word_keywords = tuple(word for word in keywords if word not in fragment_ids)
    keyword_semantic, query_semantic = _semantic_hits(
        connection=connection,
        user_id=user_id,
        semantic=semantic,
        embeddings=(
            semantic.keywords_embedding if semantic and word_keywords else None,
            semantic.query_embedding if semantic else None,
        ),
        sources=sources,
        pinned_revisions=pinned_revisions,
        candidate_limit=candidate_limit,
    )
    keyword_hits = rank_keyword_hits(
        exact=exact_hits,
        lexical=(
            _lexical_hits(
                connection=connection,
                user_id=user_id,
                query=" ".join(word_keywords),
                sources=sources,
                pinned_revisions=pinned_revisions,
                candidate_limit=candidate_limit,
            )
            if word_keywords
            else ()
        ),
        semantic=keyword_semantic,
        center_time=parsed_center_time,
        radius_hours=radius_hours,
    )
    query_hits = rank_query_hits(
        semantic=query_semantic,
        center_time=parsed_center_time,
        radius_hours=radius_hours,
    )
    return _build_results_and_epoch(
        ranked=select_memory_candidates(
            keyword_hits=keyword_hits, query_hits=query_hits, limit=limit
        ),
        run_id=run_id,
        user_id=user_id,
    )


def merge_memory_search_context(
    *,
    results: list[MemorySearchResult],
    current_epoch: MemoryContextEpoch | None,
    search_epoch: MemoryContextEpoch,
) -> tuple[list[MemorySearchResult], MemoryContextEpoch]:
    if current_epoch is None:
        return [
            cast(MemorySearchResult, dict(result)) for result in results
        ], search_epoch
    current_by_fragment = {item.fragment_id: item for item in current_epoch.items}
    canonical_results: list[MemorySearchResult] = []
    for result, searched_item in zip(results, search_epoch.items, strict=True):
        canonical_result = cast(MemorySearchResult, dict(result))
        current_item = current_by_fragment.get(searched_item.fragment_id)
        if current_item is not None:
            canonical_result["context_handle"] = current_item.item.context_handle
        canonical_results.append(canonical_result)
    return canonical_results, merge_memory_context_epochs(
        base=current_epoch,
        added=search_epoch,
    )


def _sources_for_focus(focus: MemorySearchFocus) -> tuple[MemorySource, ...]:
    configured = MEMORY_SEARCH_FOCUS_SOURCES.get(focus)
    if configured is None:
        raise ValueError("memory search focus is invalid")
    return tuple(MEMORY_SEARCH_TO_CATALOG_SOURCE[source] for source in configured)


def _load_visible_revision_ids(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
) -> tuple[str, ...]:
    source_placeholders = ", ".join("?" for _ in sources)
    revision_predicate, revision_parameters = revision_visibility_predicate(
        sources=sources,
        pinned_revisions=pinned_revisions,
    )
    rows = connection.execute(
        f"""
        SELECT revisions.revision_id
        FROM memory_revisions AS revisions
        JOIN memory_nodes AS nodes
          ON nodes.user_id = revisions.user_id
         AND nodes.node_id = revisions.node_id
        WHERE nodes.user_id = ?
          AND nodes.lifecycle = 'active' AND nodes.integrity = 'healthy'
          AND nodes.source_type IN ({source_placeholders})
          AND ({revision_predicate})
        ORDER BY revisions.revision_id
        """,
        (user_id, *sources, *revision_parameters),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _exact_hits(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    keywords: tuple[str, ...],
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
    candidate_limit: int,
) -> tuple[MemoryCandidateHit, ...]:
    visibility, visibility_parameters = visible_fragment_predicate(
        user_id=user_id,
        sources=sources,
        pinned_revisions=pinned_revisions,
    )
    placeholders = ", ".join("?" for _ in keywords)
    rows = connection.execute(
        f"""
        SELECT {MEMORY_FRAGMENT_COLUMNS}
        FROM memory_fragments AS fragments
        {VISIBLE_FRAGMENT_JOINS}
        WHERE {visibility}
          AND (
            fragments.fragment_id IN ({placeholders})
            OR fragments.revision_id IN ({placeholders})
            OR nodes.node_id IN ({placeholders})
            OR nodes.source_record_id IN ({placeholders})
            OR fragments.source_path IN ({placeholders})
          )
        ORDER BY CASE WHEN fragments.fragment_id IN ({placeholders}) THEN 0 ELSE 1 END,
                 fragments.source_path, fragments.block_index
        LIMIT ?
        """,
        (*visibility_parameters, *(keywords * 6), candidate_limit),
    ).fetchall()
    return tuple(_candidate_hit(row=row, rank=rank) for rank, row in enumerate(rows))


def _lexical_hits(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    query: str,
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
    candidate_limit: int,
) -> tuple[MemoryCandidateHit, ...]:
    rows = search_lexical_fragments(
        connection,
        user_id=user_id,
        query=query,
        sources=sources,
        pinned_revisions=pinned_revisions,
        candidate_limit=candidate_limit,
    )
    return tuple(_candidate_hit(row=row, rank=rank) for rank, row in enumerate(rows))


def _semantic_hits(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    semantic: SemanticSearchInput | None,
    embeddings: tuple[Sequence[float] | None, ...],
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
    candidate_limit: int,
) -> tuple[tuple[MemoryCandidateHit, ...], ...]:
    """One hit list per embedding; an absent embedding gets an empty list."""
    present = tuple(embedding for embedding in embeddings if embedding is not None)
    if semantic is None or not present:
        return tuple(() for _ in embeddings)
    lanes = iter(
        search_semantic_fragments(
            connection,
            user_id=user_id,
            visible_revision_ids=_load_visible_revision_ids(
                connection=connection,
                user_id=user_id,
                sources=sources,
                pinned_revisions=pinned_revisions,
            ),
            query_embeddings=present,
            embedding_generation=semantic.generation,
            candidate_limit=candidate_limit,
        )
    )
    return tuple(
        _visible_hits(
            connection=connection,
            user_id=user_id,
            matches=next(lanes),
            sources=sources,
            pinned_revisions=pinned_revisions,
        )
        if embedding is not None
        else ()
        for embedding in embeddings
    )


def _visible_hits(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    matches: tuple[SemanticFragmentMatch, ...],
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
) -> tuple[MemoryCandidateHit, ...]:
    rows = _load_visible_fragment_matches(
        connection=connection,
        user_id=user_id,
        fragment_ids=tuple(match.fragment_id for match in matches),
        sources=sources,
        pinned_revisions=pinned_revisions,
    )
    rows_by_fragment_id = {str(row["fragment_id"]): row for row in rows}
    if len(rows_by_fragment_id) != len(matches) or any(
        str(rows_by_fragment_id[match.fragment_id]["content_sha256"])
        != match.content_sha256
        for match in matches
        if match.fragment_id in rows_by_fragment_id
    ):
        raise MemoryEmbeddingIndexUnavailableError(
            "semantic memory candidates are no longer visible"
        )
    return tuple(
        _candidate_hit(row=rows_by_fragment_id[match.fragment_id], rank=rank)
        for rank, match in enumerate(matches)
    )


def _load_visible_fragment_matches(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    fragment_ids: tuple[str, ...],
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
) -> tuple[sqlite3.Row, ...]:
    if not fragment_ids:
        return ()
    fragment_placeholders = ", ".join("?" for _ in fragment_ids)
    visibility, visibility_parameters = visible_fragment_predicate(
        user_id=user_id,
        sources=sources,
        pinned_revisions=pinned_revisions,
    )
    rows = connection.execute(
        f"""
        SELECT {MEMORY_FRAGMENT_COLUMNS}
        FROM memory_fragments AS fragments
        {VISIBLE_FRAGMENT_JOINS}
        WHERE {visibility}
          AND fragments.fragment_id IN ({fragment_placeholders})
        """,
        (*visibility_parameters, *fragment_ids),
    ).fetchall()
    return tuple(rows)


def _candidate_hit(*, row: sqlite3.Row, rank: int) -> MemoryCandidateHit:
    node = _node(row)
    return MemoryCandidateHit(
        node=node,
        fragment=_fragment(row),
        label=MEMORY_CATALOG_TO_SEARCH_SOURCE[node.source],
        updated_at=str(row["updated_at"]),
        rank=rank,
    )


def _build_results_and_epoch(
    *,
    ranked: tuple[RankedMemoryCandidate, ...],
    run_id: str,
    user_id: str,
) -> tuple[list[MemorySearchResult], MemoryContextEpoch]:
    visible = tuple(
        (candidate.node, candidate.fragment, candidate.label) for candidate in ranked
    )
    epoch = build_memory_context_epoch(
        run_id=run_id,
        user_id=user_id,
        visible=visible,
    )
    results: list[MemorySearchResult] = []
    for candidate, item in zip(ranked, epoch.items, strict=True):
        results.append(
            {
                "source": candidate.label,
                "record_id": candidate.node.source_record_id,
                "content": candidate.fragment.content_text,
                "source_path": candidate.fragment.source_path,
                "heading_path": candidate.fragment.heading_path,
                "observed_at": candidate.updated_at,
                "matched_by": candidate.matched_by,
                "context_handle": item.item.context_handle,
            }
        )
    return results, epoch


def _node(row: sqlite3.Row) -> MemoryNode:
    return MemoryNode(
        user_id=str(row["user_id"]),
        node_id=str(row["node_id"]),
        source=cast(MemorySource, str(row["source_type"])),
        source_record_id=str(row["source_record_id"]),
        lifecycle="active",
        integrity="healthy",
        current_revision_id=str(row["revision_id"]),
    )


def _fragment(row: sqlite3.Row) -> MemoryFragment:
    return MemoryFragment(
        user_id=str(row["user_id"]),
        fragment_id=str(row["fragment_id"]),
        revision_id=str(row["revision_id"]),
        source_path=str(row["source_path"]),
        block_kind=cast(MemoryBlockKind, str(row["block_kind"])),
        block_index=int(row["block_index"]),
        heading_path=(
            str(row["heading_path"]) if row["heading_path"] is not None else None
        ),
        content_text=str(row["content_text"]),
        content_sha256=str(row["content_sha256"]),
    )

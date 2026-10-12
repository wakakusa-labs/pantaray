from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pantaray_agents.utils.timestamps import parse_iso8601_utc

from .models import (
    MemoryFragment,
    MemoryNode,
    MemorySearchMatchedBy,
    MemorySearchSource,
)

ACTIVITY_SOURCES = frozenset(
    {"activity_log", "activity_summary", "short_term_insight", "source_records"}
)
TEMPORAL_RERANK_WINDOW = 3
# Reciprocal rank fusion adds weight / (RRF_K + rank) per lane, so lanes whose
# scores are not comparable (bm25, cosine distance) are combined by rank alone.
# 60 is the constant of the original RRF paper.
RRF_K = 60
# Keywords are matched as written first; their embedding only lifts the word
# matches that also mean what the keywords mean. Measured on 50 recorded
# searches: 0.3 and 0.5 tie, and 0 loses about half a relevant result in eight.
KEYWORD_SEMANTIC_WEIGHT = 0.5


@dataclass(frozen=True, slots=True)
class MemoryCandidateHit:
    node: MemoryNode
    fragment: MemoryFragment
    label: MemorySearchSource
    updated_at: str
    rank: int


@dataclass(frozen=True, slots=True)
class RankedMemoryCandidate:
    node: MemoryNode
    fragment: MemoryFragment
    label: MemorySearchSource
    updated_at: str
    matched_by: MemorySearchMatchedBy


def rank_keyword_hits(
    *,
    exact: Sequence[MemoryCandidateHit],
    lexical: Sequence[MemoryCandidateHit],
    semantic: Sequence[MemoryCandidateHit],
    center_time: datetime | None,
    radius_hours: int | None,
) -> tuple[MemoryCandidateHit, ...]:
    """Exact identity matches first, then the fused word and meaning matches."""

    exact_ids = {hit.fragment.fragment_id for hit in exact}
    fused = _fuse(
        (
            (
                1.0,
                tuple(
                    hit for hit in lexical if hit.fragment.fragment_id not in exact_ids
                ),
            ),
            (
                KEYWORD_SEMANTIC_WEIGHT,
                tuple(
                    hit for hit in semantic if hit.fragment.fragment_id not in exact_ids
                ),
            ),
        )
    )
    return (
        *exact,
        *_rerank_activity_windows(
            candidates=fused, center_time=center_time, radius_hours=radius_hours
        ),
    )


def rank_query_hits(
    *,
    semantic: Sequence[MemoryCandidateHit],
    center_time: datetime | None,
    radius_hours: int | None,
) -> tuple[MemoryCandidateHit, ...]:
    return _rerank_activity_windows(
        candidates=tuple(semantic), center_time=center_time, radius_hours=radius_hours
    )


def select_memory_candidates(
    *,
    keyword_hits: Sequence[MemoryCandidateHit],
    query_hits: Sequence[MemoryCandidateHit],
    limit: int,
) -> tuple[RankedMemoryCandidate, ...]:
    """Half the slots from the top of each list, never re-sorted across them.

    A fragment both lists hold takes one slot, and a list that runs short leaves
    its slots to the other.
    """
    if limit <= 0:
        raise ValueError("memory search limit must be positive")
    keyword_ids = {hit.fragment.fragment_id for hit in keyword_hits}
    query_ids = {hit.fragment.fragment_id for hit in query_hits}
    keyword_share = limit - limit // 2 if query_hits else limit
    picked = list(keyword_hits[:keyword_share])
    taken = {hit.fragment.fragment_id for hit in picked}
    picked_query = [hit for hit in query_hits if hit.fragment.fragment_id not in taken][
        : limit - len(picked)
    ]
    taken.update(hit.fragment.fragment_id for hit in picked_query)
    picked.extend(
        [hit for hit in keyword_hits if hit.fragment.fragment_id not in taken][
            : limit - len(picked) - len(picked_query)
        ]
    )
    picked.extend(picked_query)
    return tuple(
        RankedMemoryCandidate(
            node=hit.node,
            fragment=hit.fragment,
            label=hit.label,
            updated_at=hit.updated_at,
            matched_by=_matched_by(
                hit.fragment.fragment_id, keyword_ids=keyword_ids, query_ids=query_ids
            ),
        )
        for hit in picked
    )


def _fuse(
    weighted_lanes: tuple[tuple[float, tuple[MemoryCandidateHit, ...]], ...],
) -> tuple[MemoryCandidateHit, ...]:
    scores: dict[str, float] = {}
    hits: dict[str, MemoryCandidateHit] = {}
    for weight, lane in weighted_lanes:
        for hit in lane:
            fragment_id = hit.fragment.fragment_id
            scores[fragment_id] = scores.get(fragment_id, 0.0) + weight / (
                RRF_K + hit.rank + 1
            )
            hits.setdefault(fragment_id, hit)
    return tuple(
        hits[fragment_id]
        for fragment_id in sorted(scores, key=lambda key: (-scores[key], key))
    )


def _matched_by(
    fragment_id: str, *, keyword_ids: set[str], query_ids: set[str]
) -> MemorySearchMatchedBy:
    if fragment_id in keyword_ids and fragment_id in query_ids:
        return "both"
    return "keywords" if fragment_id in keyword_ids else "query"


def _rerank_activity_windows(
    *,
    candidates: tuple[MemoryCandidateHit, ...],
    center_time: datetime | None,
    radius_hours: int | None,
) -> tuple[MemoryCandidateHit, ...]:
    reranked: list[MemoryCandidateHit] = []
    for start in range(0, len(candidates), TEMPORAL_RERANK_WINDOW):
        window = list(candidates[start : start + TEMPORAL_RERANK_WINDOW])
        activity_positions = [
            index
            for index, candidate in enumerate(window)
            if candidate.node.source in ACTIVITY_SOURCES
        ]
        # sorted() is stable, so equal times keep the window's relevance order.
        activity_candidates = sorted(
            (window[index] for index in activity_positions),
            key=lambda item: _time_prior_key(
                candidate=item,
                center_time=center_time,
                radius_hours=radius_hours,
            ),
        )
        for index, candidate in zip(
            activity_positions, activity_candidates, strict=True
        ):
            window[index] = candidate
        reranked.extend(window)
    return tuple(reranked)


def _time_prior_key(
    *,
    candidate: MemoryCandidateHit,
    center_time: datetime | None,
    radius_hours: int | None,
) -> tuple[int, float]:
    updated_at = parse_iso8601_utc(candidate.updated_at)
    if center_time is None or radius_hours is None:
        return (0, -updated_at.timestamp())
    distance_seconds = abs((updated_at - center_time).total_seconds())
    radius_seconds = radius_hours * 3600
    return (0 if distance_seconds <= radius_seconds else 1, distance_seconds)

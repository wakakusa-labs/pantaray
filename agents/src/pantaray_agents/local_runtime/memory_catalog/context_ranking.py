from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from pantaray_agents.utils.timestamps import parse_iso8601_utc

from .models import (
    MemoryFragment,
    MemoryNode,
    MemorySearchMatchKind,
    MemorySearchSource,
)

SearchLane = Literal["exact", "lexical", "semantic"]

ACTIVITY_SOURCES = frozenset(
    {"activity_log", "activity_summary", "short_term_insight", "source_records"}
)
TEMPORAL_RERANK_WINDOW = 3


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
    exact_rank: int | None = None
    lexical_rank: int | None = None
    semantic_rank: int | None = None
    match_kind: MemorySearchMatchKind | None = None


def merge_candidate_lanes(
    *,
    exact: tuple[MemoryCandidateHit, ...],
    lexical: tuple[MemoryCandidateHit, ...],
    semantic: tuple[MemoryCandidateHit, ...],
) -> tuple[RankedMemoryCandidate, ...]:
    merged: dict[str, RankedMemoryCandidate] = {}
    lanes: tuple[
        tuple[SearchLane, tuple[MemoryCandidateHit, ...]],
        ...,
    ] = (
        ("exact", exact),
        ("lexical", lexical),
        ("semantic", semantic),
    )
    for lane, hits in lanes:
        for hit in hits:
            fragment_id = hit.fragment.fragment_id
            existing = merged.get(fragment_id)
            if existing is None:
                existing = RankedMemoryCandidate(
                    node=hit.node,
                    fragment=hit.fragment,
                    label=hit.label,
                    updated_at=hit.updated_at,
                )
            else:
                _require_same_candidate(existing=existing, hit=hit)
            merged[fragment_id] = _with_lane_rank(
                candidate=existing,
                lane=lane,
                rank=hit.rank,
            )
    return tuple(merged.values())


def rank_memory_candidates(
    *,
    candidates: tuple[RankedMemoryCandidate, ...],
    center_time: datetime | None,
    radius_hours: int | None,
    limit: int,
) -> tuple[RankedMemoryCandidate, ...]:
    if limit <= 0:
        raise ValueError("memory search limit must be positive")
    if (center_time is None) != (radius_hours is None):
        raise ValueError("memory search time hint must be complete")
    if radius_hours is not None and radius_hours <= 0:
        raise ValueError("memory search time hint radius must be positive")

    classified = tuple(_classify(candidate) for candidate in candidates)
    exact = _rank_kind(
        candidates=classified,
        match_kind="exact",
        center_time=None,
        radius_hours=None,
        apply_time_prior=False,
    )
    corroborated = _rank_kind(
        candidates=classified,
        match_kind="corroborated",
        center_time=center_time,
        radius_hours=radius_hours,
        apply_time_prior=True,
    )
    lexical = _rank_kind(
        candidates=classified,
        match_kind="lexical",
        center_time=center_time,
        radius_hours=radius_hours,
        apply_time_prior=True,
    )
    semantic = _rank_kind(
        candidates=classified,
        match_kind="semantic",
        center_time=center_time,
        radius_hours=radius_hours,
        apply_time_prior=True,
    )
    return (*exact, *corroborated, *lexical, *semantic)[:limit]


def _classify(candidate: RankedMemoryCandidate) -> RankedMemoryCandidate:
    if candidate.exact_rank is not None:
        match_kind: MemorySearchMatchKind = "exact"
    elif candidate.lexical_rank is not None and candidate.semantic_rank is not None:
        match_kind = "corroborated"
    elif candidate.lexical_rank is not None:
        match_kind = "lexical"
    elif candidate.semantic_rank is not None:
        match_kind = "semantic"
    else:
        raise ValueError("memory search candidate has no retrieval lane")
    return replace(candidate, match_kind=match_kind)


def _rank_kind(
    *,
    candidates: tuple[RankedMemoryCandidate, ...],
    match_kind: MemorySearchMatchKind,
    center_time: datetime | None,
    radius_hours: int | None,
    apply_time_prior: bool,
) -> tuple[RankedMemoryCandidate, ...]:
    ordered = tuple(
        sorted(
            (item for item in candidates if item.match_kind == match_kind),
            key=_base_rank_key,
        )
    )
    if not apply_time_prior:
        return ordered
    return _rerank_activity_windows(
        candidates=ordered,
        center_time=center_time,
        radius_hours=radius_hours,
    )


def _base_rank_key(candidate: RankedMemoryCandidate) -> tuple[int, int, str]:
    if candidate.match_kind == "exact":
        assert candidate.exact_rank is not None
        return candidate.exact_rank, 0, candidate.fragment.fragment_id
    if candidate.match_kind == "corroborated":
        assert candidate.lexical_rank is not None
        assert candidate.semantic_rank is not None
        return (
            candidate.lexical_rank + candidate.semantic_rank,
            candidate.lexical_rank,
            candidate.fragment.fragment_id,
        )
    if candidate.match_kind == "lexical":
        assert candidate.lexical_rank is not None
        return candidate.lexical_rank, 0, candidate.fragment.fragment_id
    if candidate.match_kind == "semantic":
        assert candidate.semantic_rank is not None
        return candidate.semantic_rank, 0, candidate.fragment.fragment_id
    raise ValueError("memory search candidate has no match kind")


def _rerank_activity_windows(
    *,
    candidates: tuple[RankedMemoryCandidate, ...],
    center_time: datetime | None,
    radius_hours: int | None,
) -> tuple[RankedMemoryCandidate, ...]:
    reranked: list[RankedMemoryCandidate] = []
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
    candidate: RankedMemoryCandidate,
    center_time: datetime | None,
    radius_hours: int | None,
) -> tuple[int, float]:
    updated_at = parse_iso8601_utc(candidate.updated_at)
    if center_time is None or radius_hours is None:
        return (0, -updated_at.timestamp())
    distance_seconds = abs((updated_at - center_time).total_seconds())
    radius_seconds = radius_hours * 3600
    return (0 if distance_seconds <= radius_seconds else 1, distance_seconds)


def _with_lane_rank(
    *,
    candidate: RankedMemoryCandidate,
    lane: SearchLane,
    rank: int,
) -> RankedMemoryCandidate:
    if rank < 0:
        raise ValueError("memory search lane rank must not be negative")
    if lane == "exact":
        return replace(candidate, exact_rank=rank)
    if lane == "lexical":
        return replace(candidate, lexical_rank=rank)
    return replace(candidate, semantic_rank=rank)


def _require_same_candidate(
    *, existing: RankedMemoryCandidate, hit: MemoryCandidateHit
) -> None:
    if (
        existing.node != hit.node
        or existing.fragment != hit.fragment
        or existing.label != hit.label
        or existing.updated_at != hit.updated_at
    ):
        raise ValueError("memory search lanes disagree on fragment identity")

from __future__ import annotations

from pantaray_agents.local_runtime.memory_catalog.context_ranking import (
    MemoryCandidateHit,
    merge_candidate_lanes,
    rank_memory_candidates,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryFragment,
    MemoryNode,
)


def test_ranking_keeps_match_classes_separate_without_raw_score_fusion() -> None:
    exact = _hit("exact", source="fact", rank=8)
    corroborated_lexical = _hit("both", source="fact", rank=3)
    corroborated_semantic = _hit("both", source="fact", rank=1)
    lexical = _hit("lexical", source="fact", rank=0)
    semantic = _hit("semantic", source="fact", rank=0)

    ranked = rank_memory_candidates(
        candidates=merge_candidate_lanes(
            exact=(exact,),
            lexical=(corroborated_lexical, lexical),
            semantic=(corroborated_semantic, semantic),
        ),
        center_time=None,
        radius_hours=None,
        limit=10,
    )

    assert [item.fragment.fragment_id for item in ranked] == [
        "fragment-exact",
        "fragment-both",
        "fragment-lexical",
        "fragment-semantic",
    ]
    assert [item.match_kind for item in ranked] == [
        "exact",
        "corroborated",
        "lexical",
        "semantic",
    ]


def test_ranking_fills_remaining_limit_with_semantic_results() -> None:
    semantic = tuple(
        _hit(f"semantic-{index}", source="fact", rank=index) for index in range(4)
    )

    ranked = rank_memory_candidates(
        candidates=merge_candidate_lanes(exact=(), lexical=(), semantic=semantic),
        center_time=None,
        radius_hours=None,
        limit=10,
    )

    assert [item.fragment.fragment_id for item in ranked] == [
        "fragment-semantic-0",
        "fragment-semantic-1",
        "fragment-semantic-2",
        "fragment-semantic-3",
    ]


def test_time_prior_reorders_only_activity_candidates_in_bounded_window() -> None:
    old_activity = _hit(
        "old-activity",
        source="activity_log",
        rank=0,
        updated_at="2026-01-01T00:00:00Z",
    )
    stable_fact = _hit(
        "stable-fact",
        source="fact",
        rank=1,
        updated_at="2020-01-01T00:00:00Z",
    )
    new_activity = _hit(
        "new-activity",
        source="activity_log",
        rank=2,
        updated_at="2026-02-01T00:00:00Z",
    )

    ranked = rank_memory_candidates(
        candidates=merge_candidate_lanes(
            exact=(),
            lexical=(old_activity, stable_fact, new_activity),
            semantic=(),
        ),
        center_time=None,
        radius_hours=None,
        limit=10,
    )

    assert [item.fragment.fragment_id for item in ranked] == [
        "fragment-new-activity",
        "fragment-stable-fact",
        "fragment-old-activity",
    ]


def test_time_prior_keeps_relevance_order_for_activity_with_equal_times() -> None:
    # Millisecond timestamps tie for records written together; the fragment id
    # must not outrank relevance then.
    near = _hit(
        "z-near",
        source="activity_log",
        rank=0,
        updated_at="2026-01-01T00:00:00.000Z",
    )
    far = _hit(
        "a-far",
        source="activity_log",
        rank=1,
        updated_at="2026-01-01T00:00:00.000Z",
    )

    ranked = rank_memory_candidates(
        candidates=merge_candidate_lanes(exact=(), lexical=(), semantic=(near, far)),
        center_time=None,
        radius_hours=None,
        limit=10,
    )

    assert [item.fragment.fragment_id for item in ranked] == [
        "fragment-z-near",
        "fragment-a-far",
    ]


def test_time_prior_never_reorders_exact_activity_candidates() -> None:
    old_activity = _hit(
        "old-activity",
        source="activity_log",
        rank=0,
        updated_at="2026-01-01T00:00:00Z",
    )
    new_activity = _hit(
        "new-activity",
        source="activity_log",
        rank=1,
        updated_at="2026-02-01T00:00:00Z",
    )

    ranked = rank_memory_candidates(
        candidates=merge_candidate_lanes(
            exact=(old_activity, new_activity),
            lexical=(),
            semantic=(),
        ),
        center_time=None,
        radius_hours=None,
        limit=10,
    )

    assert [item.fragment.fragment_id for item in ranked] == [
        "fragment-old-activity",
        "fragment-new-activity",
    ]


def _hit(
    name: str,
    *,
    source: str,
    rank: int,
    updated_at: str = "2026-01-01T00:00:00Z",
) -> MemoryCandidateHit:
    node = MemoryNode(
        user_id="user-1",
        node_id=f"node-{name}",
        source=source,  # type: ignore[arg-type]
        source_record_id=f"record-{name}",
        lifecycle="active",
        integrity="healthy",
        current_revision_id=f"revision-{name}",
    )
    fragment = MemoryFragment(
        user_id="user-1",
        fragment_id=f"fragment-{name}",
        revision_id=f"revision-{name}",
        source_path=f"{name}.md",
        block_kind="paragraph",
        block_index=1,
        heading_path=None,
        content_text=name,
        content_sha256=name,
    )
    label = "activity_description" if source == "activity_log" else "facts"
    return MemoryCandidateHit(
        node=node,
        fragment=fragment,
        label=label,
        updated_at=updated_at,
        rank=rank,
    )

from __future__ import annotations

from pantaray_agents.local_runtime.memory_catalog.context_ranking import (
    MemoryCandidateHit,
    rank_keyword_hits,
    rank_query_hits,
    select_memory_candidates,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryFragment,
    MemoryNode,
)


def _ids(hits: tuple[MemoryCandidateHit, ...]) -> list[str]:
    return [hit.fragment.fragment_id for hit in hits]


def test_keyword_list_puts_exact_first_and_fuses_word_and_meaning_ranks() -> None:
    ranked = rank_keyword_hits(
        exact=(_hit("exact", source="fact", rank=0),),
        lexical=(
            _hit("words-only", source="fact", rank=0),
            _hit("both", source="fact", rank=1),
            _hit("exact", source="fact", rank=2),
        ),
        semantic=(_hit("both", source="fact", rank=0),),
        center_time=None,
        radius_hours=None,
    )

    assert _ids(ranked) == ["fragment-exact", "fragment-both", "fragment-words-only"]


def test_selection_takes_half_from_each_list_and_counts_a_shared_hit_once() -> None:
    keyword_hits = tuple(
        _hit(name, source="fact", rank=0) for name in ("k1", "shared", "k2", "k3")
    )
    query_hits = tuple(
        _hit(name, source="fact", rank=0) for name in ("shared", "q1", "q2", "q3")
    )

    selected = select_memory_candidates(
        keyword_hits=keyword_hits, query_hits=query_hits, limit=4
    )

    assert [item.fragment.fragment_id for item in selected] == [
        "fragment-k1",
        "fragment-shared",
        "fragment-q1",
        "fragment-q2",
    ]
    assert [item.matched_by for item in selected] == [
        "keywords",
        "both",
        "query",
        "query",
    ]


def test_selection_gives_a_short_list_s_slots_to_the_other() -> None:
    keyword_hits = tuple(
        _hit(f"k{index}", source="fact", rank=index) for index in range(4)
    )

    selected = select_memory_candidates(
        keyword_hits=keyword_hits,
        query_hits=(_hit("q0", source="fact", rank=0),),
        limit=4,
    )
    query_only = select_memory_candidates(
        keyword_hits=(), query_hits=keyword_hits, limit=3
    )

    assert [item.fragment.fragment_id for item in selected] == [
        "fragment-k0",
        "fragment-k1",
        "fragment-k2",
        "fragment-q0",
    ]
    assert [item.fragment.fragment_id for item in query_only] == [
        "fragment-k0",
        "fragment-k1",
        "fragment-k2",
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

    ranked = rank_query_hits(
        semantic=(old_activity, stable_fact, new_activity),
        center_time=None,
        radius_hours=None,
    )

    assert _ids(ranked) == [
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

    ranked = rank_query_hits(semantic=(near, far), center_time=None, radius_hours=None)

    assert _ids(ranked) == [
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

    ranked = rank_keyword_hits(
        exact=(old_activity, new_activity),
        lexical=(),
        semantic=(),
        center_time=None,
        radius_hours=None,
    )

    assert _ids(ranked) == [
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

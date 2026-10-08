"""Which memory fragments a user may see, for every memory search lane."""

from __future__ import annotations

from collections.abc import Mapping

from .models import MemorySource

# The predicates below read these aliases, so a query that carries one must join
# through them and select fragments from memory_fragments AS fragments.
VISIBLE_FRAGMENT_JOINS = """
JOIN memory_revisions AS revisions
  ON revisions.user_id = fragments.user_id
 AND revisions.revision_id = fragments.revision_id
JOIN memory_nodes AS nodes
  ON nodes.user_id = revisions.user_id
 AND nodes.node_id = revisions.node_id
""".strip()

# The same joins, walked from the nodes: a lane that tests every row's content
# reads only the fragments of visible revisions, not of every past revision,
# and in the nodes' order. CROSS JOIN fixes that order for SQLite's planner.
VISIBLE_FRAGMENTS_FROM_NODES = """
memory_nodes AS nodes
CROSS JOIN memory_revisions AS revisions
  ON revisions.user_id = nodes.user_id
 AND revisions.node_id = nodes.node_id
CROSS JOIN memory_fragments AS fragments
  ON fragments.user_id = revisions.user_id
 AND fragments.revision_id = revisions.revision_id
""".strip()

MEMORY_FRAGMENT_COLUMNS = """
nodes.user_id, nodes.node_id, nodes.source_type,
nodes.source_record_id, nodes.lifecycle, nodes.integrity,
nodes.current_revision_id, nodes.updated_at,
fragments.fragment_id, fragments.revision_id,
fragments.source_path, fragments.block_kind,
fragments.block_index, fragments.heading_path,
fragments.content_text, fragments.content_sha256
""".strip()


def visible_fragment_predicate(
    *,
    user_id: str,
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
) -> tuple[str, tuple[object, ...]]:
    source_placeholders = ", ".join("?" for _ in sources)
    revision_predicate, revision_parameters = revision_visibility_predicate(
        sources=sources,
        pinned_revisions=pinned_revisions,
    )
    predicate = f"""nodes.user_id = ?
          AND nodes.lifecycle = 'active' AND nodes.integrity = 'healthy'
          AND nodes.source_type IN ({source_placeholders})
          AND ({revision_predicate})
          AND fragments.block_kind NOT IN ('record_root', 'document_root')"""
    return predicate, (user_id, *sources, *revision_parameters)


def revision_visibility_predicate(
    *,
    sources: tuple[MemorySource, ...],
    pinned_revisions: Mapping[MemorySource, str | None] | None,
) -> tuple[str, list[object]]:
    if pinned_revisions is None:
        return "nodes.current_revision_id = revisions.revision_id", []
    current_sources = tuple(
        source for source in sources if source not in pinned_revisions
    )
    predicates: list[str] = []
    parameters: list[object] = []
    if current_sources:
        placeholders = ", ".join("?" for _ in current_sources)
        predicates.append(
            "(nodes.source_type IN ("
            + placeholders
            + ") AND nodes.current_revision_id = revisions.revision_id)"
        )
        parameters.extend(current_sources)
    for source in sources:
        if source not in pinned_revisions:
            continue
        revision_id = pinned_revisions[source]
        if revision_id is None:
            continue
        predicates.append("(nodes.source_type = ? AND revisions.revision_id = ?)")
        parameters.extend((source, revision_id))
    return (" OR ".join(predicates) or "0"), parameters

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import get_args

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
    register_after_commit,
)

from .epoch import append_memory_context_item
from .errors import MemoryReferenceInputError, MemoryReferenceNotFoundError
from .fragment_visibility import revision_visibility_predicate
from .models import MemoryContextEpoch, MemorySource, ResolvedMemoryLink
from .observability import emit_memory_catalog_event

_ALL_SOURCES: tuple[MemorySource, ...] = get_args(MemorySource)


@dataclass(frozen=True, slots=True)
class _ReferenceSource:
    node_id: str
    revision_id: str


def follow_memory_reference(
    *,
    connection: sqlite3.Connection,
    epoch: MemoryContextEpoch,
    local_ref_id: str,
    pinned_revisions: Mapping[MemorySource, str | None] | None,
    enqueue_repair_on_failure: bool,
) -> tuple[MemoryContextEpoch, ResolvedMemoryLink]:
    """Resolve one ref of the run's visible memory by its ID alone.

    The ref is looked up in the revisions memory_search shows the same run: the
    current ones, or the pinned one for each source in ``pinned_revisions``.
    A broken target is optionally queued for repair of the ref's source node.
    """

    try:
        source = _visible_reference_source(
            connection=connection,
            user_id=epoch.user_id,
            local_ref_id=local_ref_id,
            pinned_revisions=pinned_revisions,
        )
    except MemoryReferenceInputError as exc:
        _emit_resolution_failure(epoch=epoch, error=exc)
        raise
    try:
        resolved, target_node_id = _resolve_memory_reference(
            connection=connection,
            user_id=epoch.user_id,
            source=source,
            local_ref_id=local_ref_id,
        )
    except MemoryReferenceNotFoundError as exc:
        _emit_resolution_failure(epoch=epoch, error=exc)
        if enqueue_repair_on_failure:
            with immediate_transaction(connection):
                enqueue_memory_repair(
                    connection=connection,
                    user_id=epoch.user_id,
                    node_id=source.node_id,
                    detected_revision_id=source.revision_id,
                    reason="reference_resolution",
                )
        raise
    extended_epoch, target_item = append_memory_context_item(
        epoch=epoch,
        source=resolved["target_source"],
        label=f"memory ref {local_ref_id} target",
        source_path=resolved["target_path"],
        heading_path=resolved["target_heading_path"],
        content=resolved["target_content"],
        fragment_id=resolved["target_fragment_id"],
        revision_id=resolved["target_revision_id"],
        node_id=target_node_id,
        reference_depth=1,
    )
    resolved["target_context_handle"] = target_item.item.context_handle
    return extended_epoch, resolved


def _visible_reference_source(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    local_ref_id: str,
    pinned_revisions: Mapping[MemorySource, str | None] | None,
) -> _ReferenceSource:
    revision_predicate, revision_parameters = revision_visibility_predicate(
        sources=_ALL_SOURCES, pinned_revisions=pinned_revisions
    )
    # Walked from the nodes so each visible revision's refs are read by key; a
    # scan of memory_links by ID reads every past revision's refs too.
    rows = connection.execute(
        f"""
        SELECT nodes.node_id, revisions.revision_id
        FROM memory_nodes AS nodes
        CROSS JOIN memory_revisions AS revisions
          ON revisions.user_id = nodes.user_id
         AND revisions.node_id = nodes.node_id
        CROSS JOIN memory_links AS links
          ON links.user_id = revisions.user_id
         AND links.source_revision_id = revisions.revision_id
         AND links.local_ref_id = ?
        WHERE nodes.user_id = ?
          AND nodes.lifecycle = 'active' AND nodes.integrity = 'healthy'
          AND ({revision_predicate})
        LIMIT 2
        """,
        (local_ref_id, user_id, *revision_parameters),
    ).fetchall()
    # Writers give each new ref a fresh random ID and keep it within its node,
    # so only a hand-copied mapping can put one ID in two visible revisions;
    # neither target is then the right one, as unlink_memory also decides.
    if not rows:
        raise MemoryReferenceInputError(
            f"{local_ref_id} is not a ref in current memory; copy the ID from a "
            "[[ref:...]] tag you have seen"
        )
    if len(rows) > 1:
        raise MemoryReferenceInputError(
            f"{local_ref_id} is ambiguous: several memory files carry it"
        )
    return _ReferenceSource(
        node_id=str(rows[0]["node_id"]), revision_id=str(rows[0]["revision_id"])
    )


def _resolve_memory_reference(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    source: _ReferenceSource,
    local_ref_id: str,
) -> tuple[ResolvedMemoryLink, str]:
    row = connection.execute(
        """
        SELECT links.reference_note,
               source_fragments.source_path,
               source_fragments.heading_path AS source_heading_path,
               target_fragments.fragment_id AS target_fragment_id,
               target_fragments.content_text AS target_content,
               target_fragments.source_path AS target_path,
               target_fragments.heading_path AS target_heading_path,
               target_fragments.revision_id AS target_revision_id,
               target_nodes.node_id AS target_node_id,
               target_nodes.source_type AS target_source,
               target_nodes.lifecycle AS target_lifecycle,
               target_nodes.integrity AS target_integrity,
               target_nodes.current_revision_id AS current_target_revision_id
        FROM memory_links AS links
        JOIN memory_fragments AS source_fragments
          ON source_fragments.user_id = links.user_id
         AND source_fragments.fragment_id = links.source_fragment_id
        JOIN memory_fragments AS target_fragments
          ON target_fragments.user_id = links.user_id
         AND target_fragments.fragment_id = links.target_fragment_id
        JOIN memory_revisions AS target_revisions
          ON target_revisions.user_id = target_fragments.user_id
         AND target_revisions.revision_id = target_fragments.revision_id
        JOIN memory_nodes AS target_nodes
          ON target_nodes.user_id = target_revisions.user_id
         AND target_nodes.node_id = target_revisions.node_id
        WHERE links.user_id = ? AND links.source_revision_id = ?
          AND links.local_ref_id = ?
        """,
        (user_id, source.revision_id, local_ref_id),
    ).fetchone()
    if row is None:
        raise MemoryReferenceNotFoundError("ref source or immutable target is missing")
    current_revision_id = str(row["current_target_revision_id"])
    target_revision_id = str(row["target_revision_id"])
    resolved: ResolvedMemoryLink = {
        "local_ref_id": local_ref_id,
        "reference_note": str(row["reference_note"]),
        "source_path": str(row["source_path"]),
        "source_heading_path": (
            str(row["source_heading_path"])
            if row["source_heading_path"] is not None
            else None
        ),
        "target_fragment_id": str(row["target_fragment_id"]),
        "target_source": str(row["target_source"]),  # type: ignore[typeddict-item]
        "target_content": str(row["target_content"]),
        "target_path": str(row["target_path"]),
        "target_heading_path": (
            str(row["target_heading_path"])
            if row["target_heading_path"] is not None
            else None
        ),
        "target_revision_id": target_revision_id,
        "target_is_current": target_revision_id == current_revision_id,
        "target_lifecycle": str(row["target_lifecycle"]),  # type: ignore[typeddict-item]
        "target_integrity": str(row["target_integrity"]),  # type: ignore[typeddict-item]
        "current_target_revision_id": current_revision_id,
    }
    return resolved, str(row["target_node_id"])


def _emit_resolution_failure(
    *,
    epoch: MemoryContextEpoch,
    error: MemoryReferenceInputError | MemoryReferenceNotFoundError,
) -> None:
    emit_memory_catalog_event(
        "memory_link_resolution_failed",
        user_id=epoch.user_id,
        run_id=epoch.run_id,
        fault_code=type(error).__name__,
    )


def enqueue_memory_repair(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    node_id: str,
    detected_revision_id: str,
    reason: str,
) -> None:
    now = now_utc_iso()
    cursor = connection.execute(
        """
        INSERT INTO memory_repair_queue(
            user_id, node_id, detected_revision_id, reason, state,
            attempt_count, next_attempt_at, created_at
        ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
        ON CONFLICT(user_id, node_id, detected_revision_id, reason) DO NOTHING
        """,
        (
            user_id,
            node_id,
            detected_revision_id,
            reason,
            now,
            now,
        ),
    )
    if cursor.rowcount == 1:
        register_after_commit(
            connection=connection,
            callback=lambda: emit_memory_catalog_event(
                "memory_link_repair_enqueued",
                user_id=user_id,
                node_id=node_id,
                revision_id=detected_revision_id,
                fault_code=reason,
            ),
        )

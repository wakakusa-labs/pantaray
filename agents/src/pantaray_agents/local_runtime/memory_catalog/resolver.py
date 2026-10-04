from __future__ import annotations

import sqlite3

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import (
    immediate_transaction,
    register_after_commit,
)

from .epoch import append_memory_context_item, require_reference_source
from .errors import MemoryReferenceInputError, MemoryReferenceNotFoundError
from .models import MemoryContextEpoch, ResolvedContextItem, ResolvedMemoryLink
from .observability import emit_memory_catalog_event


def follow_memory_reference(
    *,
    connection: sqlite3.Connection,
    epoch: MemoryContextEpoch,
    source_handle: str,
    local_ref_id: str,
    enqueue_repair_on_failure: bool,
) -> tuple[MemoryContextEpoch, ResolvedMemoryLink]:
    """Resolve one visible ref, optionally repairing broken persisted state."""

    source = require_reference_source(epoch=epoch, context_handle=source_handle)
    try:
        resolved = _resolve_memory_reference(
            connection=connection,
            epoch=epoch,
            source=source,
            local_ref_id=local_ref_id,
        )
        target = connection.execute(
            """
            SELECT revisions.node_id
            FROM memory_fragments AS fragments
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id
             AND revisions.revision_id = fragments.revision_id
            WHERE fragments.user_id = ? AND fragments.fragment_id = ?
            """,
            (epoch.user_id, resolved["target_fragment_id"]),
        ).fetchone()
        if target is None:
            raise MemoryReferenceNotFoundError("resolved target node is absent")
        extended_epoch, target_item = append_memory_context_item(
            epoch=epoch,
            source=resolved["target_source"],
            label=f"memory ref {resolved['local_ref_id']} target",
            source_path=resolved["target_path"],
            heading_path=resolved["target_heading_path"],
            content=resolved["target_content"],
            fragment_id=resolved["target_fragment_id"],
            revision_id=resolved["target_revision_id"],
            node_id=str(target["node_id"]),
            reference_depth=1,
        )
    except (MemoryReferenceInputError, MemoryReferenceNotFoundError) as exc:
        _emit_resolution_failure(epoch=epoch, error=exc)
        if enqueue_repair_on_failure and isinstance(exc, MemoryReferenceNotFoundError):
            with immediate_transaction(connection):
                enqueue_memory_repair(
                    connection=connection,
                    user_id=epoch.user_id,
                    node_id=source.node_id,
                    detected_revision_id=source.revision_id,
                    reason="reference_resolution",
                )
        raise
    resolved["target_context_handle"] = target_item.item.context_handle
    return extended_epoch, resolved


def _resolve_memory_reference(
    *,
    connection: sqlite3.Connection,
    epoch: MemoryContextEpoch,
    source: ResolvedContextItem,
    local_ref_id: str,
) -> ResolvedMemoryLink:
    source_row = connection.execute(
        """
        SELECT source_path, heading_path, content_text
        FROM memory_fragments
        WHERE user_id = ? AND revision_id = ? AND fragment_id = ?
        """,
        (epoch.user_id, source.revision_id, source.fragment_id),
    ).fetchone()
    if source_row is None:
        raise MemoryReferenceNotFoundError("visible source fragment is missing")
    occurrences = extract_markdown_references(str(source_row["content_text"]))
    if sum(item.local_ref_id == local_ref_id for item in occurrences) != 1:
        raise MemoryReferenceInputError(
            "visible source fragment does not contain the ref"
        )
    row = connection.execute(
        """
        SELECT links.reference_note,
               target_fragments.fragment_id AS target_fragment_id,
               target_fragments.content_text AS target_content,
               target_fragments.source_path AS target_path,
               target_fragments.heading_path AS target_heading_path,
               target_fragments.revision_id AS target_revision_id,
               target_nodes.source_type AS target_source,
               target_nodes.lifecycle AS target_lifecycle,
               target_nodes.integrity AS target_integrity,
               target_nodes.current_revision_id AS current_target_revision_id
        FROM memory_links AS links
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
          AND links.source_fragment_id = ? AND links.local_ref_id = ?
        """,
        (epoch.user_id, source.revision_id, source.fragment_id, local_ref_id),
    ).fetchone()
    if row is None:
        raise MemoryReferenceNotFoundError("ref mapping or immutable target is missing")
    current_revision_id = str(row["current_target_revision_id"])
    target_revision_id = str(row["target_revision_id"])
    return {
        "local_ref_id": local_ref_id,
        "reference_note": str(row["reference_note"]),
        "source_path": str(source_row["source_path"]),
        "source_heading_path": (
            str(source_row["heading_path"])
            if source_row["heading_path"] is not None
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

from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from . import revision_inspection
from .artifact_repair import validate_agent_experience_repair_draft
from .draft import create_memory_draft
from .errors import MemoryCatalogIntegrityError
from .lifecycle import mark_memory_corrupt
from .models import MemoryNode, MemoryRevision
from .observability import emit_memory_catalog_event
from .repair_projection import write_inline_repair_projection
from .repair_queue import RepairJob, complete_repair_job
from .repository import load_revision


def rollback_or_quarantine(
    *,
    connection: sqlite3.Connection,
    artifact_root: Path,
    node: MemoryNode,
    current_revision: MemoryRevision,
    job: RepairJob,
) -> bool:
    # Walk the parent chain: created_at ties within one millisecond, so it
    # cannot order a node's revisions.
    candidates = connection.execute(
        """
        WITH RECURSIVE ancestors(revision_id, depth) AS (
            SELECT parent_revision_id, 1 FROM memory_revision_parents
            WHERE user_id = :user_id AND child_revision_id = :revision_id
            UNION ALL
            SELECT parents.parent_revision_id, ancestors.depth + 1
            FROM memory_revision_parents AS parents
            JOIN ancestors ON parents.child_revision_id = ancestors.revision_id
            WHERE parents.user_id = :user_id
        )
        SELECT revision_id FROM ancestors ORDER BY depth
        """,
        {"user_id": node.user_id, "revision_id": current_revision.revision_id},
    ).fetchall()
    valid_revision: MemoryRevision | None = None
    for row in candidates:
        candidate_id = str(row["revision_id"])
        try:
            inspection = revision_inspection.inspect_revision(
                connection=connection,
                artifact_root=artifact_root,
                user_id=node.user_id,
                revision_id=candidate_id,
            )
        except revision_inspection.REVISION_INTEGRITY_ERRORS:
            continue
        if inspection.needs_link_repair:
            continue
        if node.source == "agent_experience":
            candidate_draft = create_memory_draft(
                user_id=node.user_id,
                owner_node_id=node.node_id,
                base_revision_id=candidate_id,
                documents=inspection.documents,
                carried_links=inspection.carried_links,
            )
            try:
                validate_agent_experience_repair_draft(
                    connection=connection,
                    draft=candidate_draft,
                    source_record_id=node.source_record_id,
                )
            except MemoryCatalogIntegrityError:
                continue
        valid_revision = load_revision(
            connection=connection, user_id=node.user_id, revision_id=candidate_id
        )
        if (
            valid_revision is not None
            and node.source in {"fact", "long_term_insight"}
            and not (valid_revision.profile_brief or "").strip()
        ):
            valid_revision = None
            continue
        break
    with immediate_transaction(connection):
        if valid_revision is None:
            mark_memory_corrupt(
                connection=connection, user_id=node.user_id, node_id=node.node_id
            )
        else:
            cursor = connection.execute(
                """
                UPDATE memory_nodes SET current_revision_id = ?, updated_at = ?
                WHERE user_id = ? AND node_id = ? AND current_revision_id = ?
                """,
                (
                    valid_revision.revision_id,
                    now_utc_iso(),
                    node.user_id,
                    node.node_id,
                    current_revision.revision_id,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryCatalogIntegrityError("memory head changed during rollback")
            _write_projection_for_revision(
                connection=connection,
                node=node,
                revision=valid_revision,
            )
        complete_repair_job(
            connection=connection,
            job=job,
            completed_at=now_utc_iso(),
        )
    if valid_revision is not None:
        emit_memory_catalog_event(
            "memory_link_repaired",
            user_id=job.user_id,
            node_id=job.node_id,
            revision_id=valid_revision.revision_id,
            fault_code=job.reason,
        )
    return True


def _write_projection_for_revision(
    *, connection: sqlite3.Connection, node: MemoryNode, revision: MemoryRevision
) -> None:
    if revision.body_kind == "inline":
        write_inline_repair_projection(
            connection=connection,
            source=node.source,
            source_record_id=node.source_record_id,
            revision=revision,
        )
        return
    from .artifact_repair import write_artifact_repair_projection

    write_artifact_repair_projection(
        connection=connection,
        revision=revision,
        source=node.source,
        source_record_id=node.source_record_id,
    )


__all__ = ["rollback_or_quarantine"]

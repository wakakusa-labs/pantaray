from __future__ import annotations

from pathlib import Path

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
    remove_reference_occurrences,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from . import repair_rollback, revision_inspection
from .artifact_repair import (
    publish_artifact_repair,
    rebuild_agent_experience_repair_draft,
)
from .connection import open_memory_catalog_connection
from .draft import create_memory_draft
from .errors import MemoryCatalogIntegrityError
from .lifecycle import mark_memory_corrupt
from .models import MemoryDocument
from .observability import emit_memory_catalog_event
from .publication import MemoryPublicationRequest, publish_inline_revision
from .repair_projection import write_inline_repair_projection
from .repair_queue import RepairJob, complete_repair_job
from .repository import load_node, load_revision


def repair_job(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    job: RepairJob,
) -> bool:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        node = load_node(
            connection=connection, user_id=job.user_id, node_id=job.node_id
        )
        if node is None or node.current_revision_id != job.detected_revision_id:
            with immediate_transaction(connection):
                complete_repair_job(
                    connection=connection,
                    job=job,
                    completed_at=now_utc_iso(),
                )
            return True
        revision = load_revision(
            connection=connection,
            user_id=job.user_id,
            revision_id=job.detected_revision_id,
        )
        if revision is None:
            with immediate_transaction(connection):
                mark_memory_corrupt(
                    connection=connection, user_id=job.user_id, node_id=job.node_id
                )
                complete_repair_job(
                    connection=connection,
                    job=job,
                    completed_at=now_utc_iso(),
                )
            return True
        if job.reason == "revision_integrity":
            return repair_rollback.rollback_or_quarantine(
                connection=connection,
                artifact_root=artifact_root,
                node=node,
                current_revision=revision,
                job=job,
            )
        inspection = revision_inspection.inspect_revision(
            connection=connection,
            artifact_root=artifact_root,
            user_id=job.user_id,
            revision_id=job.detected_revision_id,
        )
        if not inspection.needs_link_repair:
            with immediate_transaction(connection):
                complete_repair_job(
                    connection=connection,
                    job=job,
                    completed_at=now_utc_iso(),
                )
            return True
        if node.source == "agent_experience":
            try:
                draft = rebuild_agent_experience_repair_draft(
                    connection=connection,
                    user_id=job.user_id,
                    node_id=job.node_id,
                    base_revision_id=job.detected_revision_id,
                    documents=inspection.documents,
                )
            except MemoryCatalogIntegrityError:
                return repair_rollback.rollback_or_quarantine(
                    connection=connection,
                    artifact_root=artifact_root,
                    node=node,
                    current_revision=revision,
                    job=job,
                )
        else:
            repaired_documents = _remove_invalid_tags(
                documents=inspection.documents,
                invalid_anchors=inspection.invalid_anchors,
            )
            draft = create_memory_draft(
                user_id=job.user_id,
                owner_node_id=job.node_id,
                base_revision_id=job.detected_revision_id,
                documents=repaired_documents,
                carried_links=inspection.carried_links,
            )
    if revision.body_kind == "artifact_tree":
        publish_artifact_repair(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=artifact_root,
            source=node.source,
            source_record_id=node.source_record_id,
            draft=draft,
        )
    else:
        with open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection:
            with immediate_transaction(connection):
                repaired = publish_inline_revision(
                    connection=connection,
                    request=MemoryPublicationRequest(
                        source=node.source,
                        source_record_id=node.source_record_id,
                        draft=draft,
                        body_kind="inline",
                    ),
                    write_domain_projection=lambda conn, activated: (
                        write_inline_repair_projection(
                            connection=conn,
                            source=node.source,
                            source_record_id=node.source_record_id,
                            revision=activated,
                        )
                    ),
                )
                _ = repaired
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            complete_repair_job(
                connection=connection,
                job=job,
                completed_at=now_utc_iso(),
            )
    emit_memory_catalog_event(
        "memory_link_repaired",
        user_id=job.user_id,
        node_id=job.node_id,
        revision_id=job.detected_revision_id,
        fault_code=job.reason,
    )
    return True


def _remove_invalid_tags(
    *,
    documents: tuple[MemoryDocument, ...],
    invalid_anchors: tuple[tuple[str, int], ...],
) -> tuple[MemoryDocument, ...]:
    invalid = set(invalid_anchors)
    repaired: list[MemoryDocument] = []
    for document in documents:
        occurrences = extract_markdown_references(document.content)
        local_orders = {
            occurrence.anchor_order
            for occurrence in occurrences
            if (document.source_path, occurrence.anchor_order) in invalid
        }
        repaired.append(
            MemoryDocument(
                document.source_path,
                remove_reference_occurrences(document.content, local_orders),
            )
        )
    return tuple(repaired)


__all__ = ["repair_job"]

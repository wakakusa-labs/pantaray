from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
    remove_reference_occurrences,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.utils.ephemeral_cleanup import remove_ephemeral_tree

from .connection import open_memory_catalog_connection
from .cutover_artifacts import (
    materialize_planned_artifacts,
    promote_planned_artifacts,
)
from .cutover_cleanup import finalize_completed_cutover, parse_cutover_inventory
from .cutover_models import PlannedLink, PlannedRecord, QuarantinedLink
from .cutover_records import LegacyMemoryRecord, load_legacy_memory_records
from .cutover_reset import inspect_partial_catalog, reset_partial_catalog
from .cutover_support import (
    LegacyReferenceMapping,
    all_occurrences,
    create_preflight_backup,
    load_legacy_reference_mappings,
    record_empty_cutover,
    record_failed_cutover,
    source_markup,
    stable_node_id,
    stable_revision_id,
)
from .cutover_support import (
    occurrences_by_id as group_occurrences_by_id,
)
from .errors import MemoryCutoverError
from .fragments import (
    MEMORY_FRAGMENT_SCHEMA_VERSION,
    artifact_content_sha256,
    build_fragments,
    parse_document,
)
from .models import MemoryDocument, MemoryFragment, MemorySource

CUTOVER_NAME = "memory_catalog_v1"


def run_memory_catalog_cutover(
    *, db_path: Path, busy_timeout_ms: int, artifact_root: Path
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        state = connection.execute(
            """
            SELECT state, inventory_json FROM memory_catalog_cutovers
            WHERE cutover_name = ?
            """,
            (CUTOVER_NAME,),
        ).fetchone()
        if state is not None:
            if str(state["state"]) == "completed":
                finalize_completed_cutover(
                    db_path=db_path,
                    artifact_root=artifact_root,
                    inventory=parse_cutover_inventory(str(state["inventory_json"])),
                )
                return
            raise MemoryCutoverError(
                "Memory Catalog cutover is incomplete; restore the preflight backup"
            )
        partial_catalog = inspect_partial_catalog(
            connection=connection,
            artifact_root=artifact_root,
        )
        if partial_catalog.pending_user_erasure_count > 0:
            raise MemoryCutoverError(
                "pending user memory erasure must complete before catalog cutover"
            )
        records = load_legacy_memory_records(
            connection=connection, artifact_root=artifact_root
        )
        mappings = load_legacy_reference_mappings(connection)
    inventory: dict[str, object] = {
        "records": len(records),
        "legacy_reference_mappings": len(mappings),
        "discarded_partial_catalog_rows": partial_catalog.row_count,
        "discarded_partial_catalog_artifacts": partial_catalog.artifacts_present,
    }
    needs_backup = (
        bool(records) or partial_catalog.exists or (artifact_root / "memory").exists()
    )
    if needs_backup:
        backup = create_preflight_backup(db_path=db_path, artifact_root=artifact_root)
        inventory["backup_database"] = str(backup[0])
        inventory["backup_artifacts"] = str(backup[1])
    if partial_catalog.exists:
        try:
            reset_partial_catalog(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                artifact_root=artifact_root,
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            record_failed_cutover(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                inventory=inventory,
                error_code=type(exc).__name__,
            )
            raise MemoryCutoverError(
                "Memory Catalog partial-state reset failed; "
                "restore the recorded preflight backup"
            ) from exc
    if not records:
        record_empty_cutover(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            inventory=inventory,
        )
        finalize_completed_cutover(
            db_path=db_path,
            artifact_root=artifact_root,
            inventory=inventory,
        )
        return
    cutover_staging_root = (
        artifact_root / "memory_catalog" / "cutover_staging" / uuid.uuid4().hex
    )
    plans: tuple[PlannedRecord, ...] = ()
    promoted_paths: tuple[Path, ...] = ()
    try:
        plans = _plan_records(records=records, mappings=mappings)
        plans = materialize_planned_artifacts(
            plans=plans,
            staging_root=cutover_staging_root,
        )
        promoted_paths = promote_planned_artifacts(
            plans=plans,
            artifact_root=artifact_root,
            staging_root=cutover_staging_root,
        )
        _import_plans(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            plans=plans,
            inventory=inventory,
        )
    except (OSError, sqlite3.DatabaseError, ValueError, MemoryCutoverError) as exc:
        for path in reversed(promoted_paths):
            shutil.rmtree(path, ignore_errors=True)
        remove_ephemeral_tree(
            path=cutover_staging_root,
            operation="failed_cutover_workspace",
        )
        record_failed_cutover(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            inventory=inventory,
            error_code=type(exc).__name__,
        )
        raise MemoryCutoverError(
            "Memory Catalog cutover failed; restore the recorded preflight backup"
        ) from exc
    remove_ephemeral_tree(
        path=cutover_staging_root,
        operation="completed_cutover_workspace",
    )
    finalize_completed_cutover(
        db_path=db_path,
        artifact_root=artifact_root,
        inventory=inventory,
    )


def _plan_records(
    *,
    records: tuple[LegacyMemoryRecord, ...],
    mappings: tuple[LegacyReferenceMapping, ...],
) -> tuple[PlannedRecord, ...]:
    targets = {
        (record.user_id, key): record
        for record in records
        for key in record.memory_keys
    }
    mappings_by_owner = {
        (mapping.user_id, mapping.owner_memory_key, mapping.local_ref_id): mapping
        for mapping in mappings
    }
    plans: list[PlannedRecord] = []
    for record in records:
        owner_key = record.memory_keys[0]
        occurrences_by_id = group_occurrences_by_id(record.documents)
        links: list[PlannedLink] = []
        quarantine: list[QuarantinedLink] = []
        removals: dict[str, set[int]] = {}
        for source_path, occurrence in all_occurrences(record.documents):
            mapping = mappings_by_owner.get(
                (record.user_id, owner_key, occurrence.local_ref_id)
            )
            reason: str | None = None
            target: LegacyMemoryRecord | None = None
            if len(occurrences_by_id[occurrence.local_ref_id]) != 1:
                reason = "duplicate_local_ref_id"
            elif mapping is None:
                reason = "missing_mapping"
            elif occurrence.note is None or not occurrence.note.strip():
                reason = "missing_relationship_note"
            else:
                target = targets.get((record.user_id, mapping.target_memory_key))
                if target is None:
                    reason = "missing_target"
            if reason is not None:
                removals.setdefault(source_path, set()).add(occurrence.anchor_order)
                quarantine.append(
                    QuarantinedLink(
                        local_ref_id=occurrence.local_ref_id,
                        source_path=source_path,
                        source_markup=source_markup(
                            record.documents, source_path, occurrence
                        ),
                        target_memory_key=(
                            mapping.target_memory_key if mapping else None
                        ),
                        reason=reason,
                    )
                )
                continue
            assert mapping is not None and target is not None
            links.append(
                PlannedLink(
                    local_ref_id=occurrence.local_ref_id,
                    source_path=source_path,
                    occurrence=occurrence,
                    target=target,
                    created_at=mapping.created_at,
                )
            )
        current_documents = tuple(
            MemoryDocument(
                document.source_path,
                remove_reference_occurrences(
                    document.content, removals.get(document.source_path, set())
                ),
            )
            for document in record.documents
        )
        raw_revision_id = stable_revision_id("rev", record, record.documents)
        current_revision_id = (
            stable_revision_id("repair", record, current_documents)
            if quarantine
            else raw_revision_id
        )
        plans.append(
            PlannedRecord(
                record=record,
                node_id=stable_node_id(record),
                raw_revision_id=raw_revision_id,
                current_revision_id=current_revision_id,
                current_documents=current_documents,
                raw_artifact_path=None,
                current_artifact_path=None,
                links=tuple(links),
                quarantine=tuple(quarantine),
            )
        )
    return tuple(plans)


def _import_plans(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    plans: tuple[PlannedRecord, ...],
    inventory: dict[str, object],
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            now = now_utc_iso()
            connection.execute(
                """
                INSERT INTO memory_catalog_cutovers(
                    cutover_name, state, inventory_json, started_at
                ) VALUES (?, 'running', ?, ?)
                """,
                (CUTOVER_NAME, json.dumps(inventory, sort_keys=True), now),
            )
            fragments_by_record = _insert_records(connection=connection, plans=plans)
            _insert_links_and_quarantine(
                connection=connection,
                plans=plans,
                fragments_by_record=fragments_by_record,
            )
            _insert_evidence_edges(
                connection=connection,
                plans=plans,
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MemoryCutoverError(
                    "catalog import created foreign key violations"
                )
            inventory["quarantined_links"] = sum(len(plan.quarantine) for plan in plans)
            inventory["imported_links"] = sum(len(plan.links) for plan in plans)
            connection.execute(
                """
                UPDATE memory_catalog_cutovers
                SET state = 'completed', inventory_json = ?, completed_at = ?
                WHERE cutover_name = ? AND state = 'running'
                """,
                (
                    json.dumps(inventory, sort_keys=True),
                    now_utc_iso(),
                    CUTOVER_NAME,
                ),
            )


def _insert_records(
    *, connection: sqlite3.Connection, plans: tuple[PlannedRecord, ...]
) -> dict[tuple[str, MemorySource, str], tuple[MemoryFragment, ...]]:
    current_fragments: dict[
        tuple[str, MemorySource, str], tuple[MemoryFragment, ...]
    ] = {}
    for plan in plans:
        record = plan.record
        now = record.created_at
        connection.execute(
            """
            INSERT INTO memory_nodes(
                user_id, node_id, source_type, source_record_id, lifecycle,
                integrity, current_revision_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', 'healthy', ?, ?, ?)
            """,
            (
                record.user_id,
                plan.node_id,
                record.source,
                record.source_record_id,
                plan.current_revision_id,
                now,
                now,
            ),
        )
        raw_fragments = _insert_revision(
            connection=connection,
            plan=plan,
            revision_id=plan.raw_revision_id,
            documents=record.documents,
            artifact_path=plan.raw_artifact_path,
        )
        fragments = raw_fragments
        if plan.current_revision_id != plan.raw_revision_id:
            fragments = _insert_revision(
                connection=connection,
                plan=plan,
                revision_id=plan.current_revision_id,
                documents=plan.current_documents,
                artifact_path=plan.current_artifact_path,
            )
            connection.execute(
                """
                INSERT INTO memory_revision_parents(
                    user_id, node_id, child_revision_id, parent_revision_id
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    record.user_id,
                    plan.node_id,
                    plan.current_revision_id,
                    plan.raw_revision_id,
                ),
            )
        current_fragments[(record.user_id, record.source, record.source_record_id)] = (
            fragments
        )
    return current_fragments


def _insert_revision(
    *,
    connection: sqlite3.Connection,
    plan: PlannedRecord,
    revision_id: str,
    documents: tuple[MemoryDocument, ...],
    artifact_path: str | None,
) -> tuple[MemoryFragment, ...]:
    record = plan.record
    connection.execute(
        """
        INSERT INTO memory_revisions(
            user_id, revision_id, node_id, body_kind, inline_body,
            artifact_root_path, fragment_schema_version, content_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.user_id,
            revision_id,
            plan.node_id,
            record.body_kind,
            documents[0].content if record.body_kind == "inline" else None,
            artifact_path,
            MEMORY_FRAGMENT_SCHEMA_VERSION,
            artifact_content_sha256(documents),
            record.created_at,
        ),
    )
    fragments = build_fragments(
        user_id=record.user_id,
        revision_id=revision_id,
        documents=documents,
        include_record_root=record.body_kind == "artifact_tree",
    )
    connection.executemany(
        """
        INSERT INTO memory_fragments(
            user_id, fragment_id, revision_id, source_path, block_kind,
            block_index, heading_path, content_text, content_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                item.user_id,
                item.fragment_id,
                item.revision_id,
                item.source_path,
                item.block_kind,
                item.block_index,
                item.heading_path,
                item.content_text,
                item.content_sha256,
            )
            for item in fragments
        ),
    )
    return fragments


def _insert_links_and_quarantine(
    *,
    connection: sqlite3.Connection,
    plans: tuple[PlannedRecord, ...],
    fragments_by_record: dict[
        tuple[str, MemorySource, str], tuple[MemoryFragment, ...]
    ],
) -> None:
    for plan in plans:
        source_fragments = fragments_by_record[
            (plan.record.user_id, plan.record.source, plan.record.source_record_id)
        ]
        for link in plan.links:
            source_fragment = _source_fragment(
                documents=plan.current_documents,
                fragments=source_fragments,
                source_path=link.source_path,
                local_ref_id=link.local_ref_id,
            )
            target_fragments = fragments_by_record[
                (link.target.user_id, link.target.source, link.target.source_record_id)
            ]
            target_fragment = next(
                item
                for item in target_fragments
                if item.block_kind
                == (
                    "record_root"
                    if link.target.body_kind == "artifact_tree"
                    else "document_root"
                )
            )
            connection.execute(
                """
                INSERT INTO memory_links(
                    user_id, source_revision_id, local_ref_id, source_fragment_id,
                    target_fragment_id, reference_note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.record.user_id,
                    plan.current_revision_id,
                    link.local_ref_id,
                    source_fragment.fragment_id,
                    target_fragment.fragment_id,
                    str(link.occurrence.note),
                    link.created_at,
                ),
            )
        for item in plan.quarantine:
            connection.execute(
                """
                INSERT INTO memory_link_quarantine(
                    user_id, quarantine_id, owner_source, owner_record_id,
                    local_ref_id, source_path, source_markup,
                    legacy_target_memory_key, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.record.user_id,
                    f"quarantine_{uuid.uuid4().hex}",
                    plan.record.source,
                    plan.record.source_record_id,
                    item.local_ref_id,
                    item.source_path,
                    item.source_markup,
                    item.target_memory_key,
                    item.reason,
                    now_utc_iso(),
                ),
            )


def _insert_evidence_edges(
    *, connection: sqlite3.Connection, plans: tuple[PlannedRecord, ...]
) -> None:
    revisions_by_source_id = {
        (plan.record.user_id, plan.record.source_record_id): plan.current_revision_id
        for plan in plans
        if plan.record.source in {"activity_log", "activity_summary"}
    }
    for plan in plans:
        for source_id in plan.record.evidence_source_ids:
            source_revision_id = revisions_by_source_id.get(
                (plan.record.user_id, source_id)
            )
            if source_revision_id is None:
                raise MemoryCutoverError("activity summary evidence source is absent")
            connection.execute(
                """
                INSERT INTO memory_evidence_edges(
                    user_id, derived_revision_id, source_revision_id, evidence_role
                ) VALUES (?, ?, ?, 'summary_source')
                """,
                (plan.record.user_id, plan.current_revision_id, source_revision_id),
            )


def _source_fragment(
    *,
    documents: tuple[MemoryDocument, ...],
    fragments: tuple[MemoryFragment, ...],
    source_path: str,
    local_ref_id: str,
) -> MemoryFragment:
    document = next(item for item in documents if item.source_path == source_path)
    occurrence = next(
        item
        for item in extract_markdown_references(document.content)
        if item.local_ref_id == local_ref_id
    )
    block = next(
        item
        for item in parse_document(source_path, document.content)
        if item.block_kind != "document_root"
        and item.start_offset <= occurrence.match_start
        and occurrence.match_end <= item.end_offset
    )
    return next(
        item
        for item in fragments
        if item.source_path == source_path and item.block_index == block.block_index
    )

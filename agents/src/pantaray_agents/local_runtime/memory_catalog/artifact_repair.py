from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .agent_experience_content import (
    AGENT_EXPERIENCE_INDEX_PATH,
    experience_id_from_path,
    parse_agent_experience_evidence,
    render_action_evidence_anchor,
)
from .agent_experience_delta import parse_agent_experience_tree
from .agent_experience_validation import (
    build_agent_experience_evidence_edges,
    validate_agent_experience_evidence_links,
)
from .artifact_workspace import drop_deleted_references
from .draft import create_memory_draft
from .errors import MemoryCatalogIntegrityError
from .models import (
    DraftLink,
    MemoryDocument,
    MemoryDraftCheckpoint,
    MemoryEvidenceEdge,
    MemoryIntentKind,
    MemoryRevision,
    MemorySource,
)
from .publication import (
    MemoryPublicationRequest,
    publish_artifact_revision,
)


def publish_artifact_repair(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    source: MemorySource,
    source_record_id: str,
    draft: MemoryDraftCheckpoint,
) -> MemoryRevision:
    intent_kind = _repair_intent_kind(source)
    payload_json = build_repair_payload(
        user_id=draft.user_id, source_record_id=source_record_id
    )
    revision_id = f"rev_{uuid.uuid4().hex}" if source == "agent_experience" else None

    def build_request(connection: sqlite3.Connection) -> MemoryPublicationRequest:
        evidence_edges: tuple[MemoryEvidenceEdge, ...] = ()
        if source == "agent_experience":
            validate_agent_experience_repair_draft(
                connection=connection,
                draft=draft,
                source_record_id=source_record_id,
            )
            assert revision_id is not None
            evidence_edges = build_agent_experience_evidence_edges(
                connection=connection,
                draft=draft,
                revision_id=revision_id,
            )
        return MemoryPublicationRequest(
            source=source,
            source_record_id=source_record_id,
            draft=draft,
            body_kind="artifact_tree",
            revision_id=revision_id,
            evidence_edges=evidence_edges,
            intent_kind=intent_kind,
            domain_payload_json=payload_json,
        )

    def write_repair_projection(
        connection: sqlite3.Connection, revision: MemoryRevision
    ) -> None:
        if source == "agent_experience":
            validate_agent_experience_repair_draft(
                connection=connection,
                draft=draft,
                source_record_id=source_record_id,
            )
        write_artifact_repair_projection(
            connection=connection,
            revision=revision,
            source=source,
            source_record_id=source_record_id,
        )

    return publish_artifact_revision(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        build_request=build_request,
        write_domain_projection=write_repair_projection,
    )


def rebuild_agent_experience_repair_draft(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    node_id: str,
    base_revision_id: str,
    documents: tuple[MemoryDocument, ...],
) -> MemoryDraftCheckpoint:
    parse_agent_experience_tree(documents)
    repaired = _drop_deleted_action_evidence(
        connection=connection, user_id=user_id, documents=documents
    )
    links: list[DraftLink] = []
    seen_ref_ids: set[str] = set()
    for document in repaired:
        if document.source_path == AGENT_EXPERIENCE_INDEX_PATH:
            continue
        experience_id_from_path(document.source_path)
        for evidence in parse_agent_experience_evidence(document.content):
            if evidence.local_ref_id in seen_ref_ids:
                raise MemoryCatalogIntegrityError(
                    "Agent Experience evidence ref IDs are not unique"
                )
            seen_ref_ids.add(evidence.local_ref_id)
            target_fragment_id = _load_action_evidence_fragment(
                connection=connection,
                user_id=user_id,
                action_id=evidence.action_id,
                base_revision_id=base_revision_id,
            )
            links.append(
                DraftLink(
                    local_ref_id=evidence.local_ref_id,
                    target_fragment_id=target_fragment_id,
                    source_path=document.source_path,
                    source_anchor_text=(
                        f"{render_action_evidence_anchor(evidence.action_id).removeprefix('- ')} "
                        f'[[ref:{evidence.local_ref_id} note:"source action"]]'
                    ),
                    source_anchor_occurrence=1,
                    reference_note="source action",
                    created_at=now_utc_iso(),
                    state="carried",
                )
            )
    if not links and repaired == documents:
        raise MemoryCatalogIntegrityError(
            "Agent Experience repair has no reconstructable evidence"
        )
    return create_memory_draft(
        user_id=user_id,
        owner_node_id=node_id,
        base_revision_id=base_revision_id,
        documents=repaired,
        carried_links=tuple(links),
    )


def validate_agent_experience_repair_draft(
    *,
    connection: sqlite3.Connection,
    draft: MemoryDraftCheckpoint,
    source_record_id: str,
) -> None:
    if source_record_id != draft.user_id or draft.base_revision_id is None:
        raise MemoryCatalogIntegrityError("Agent Experience repair identity is invalid")
    base_documents = _drop_deleted_action_evidence(
        connection=connection,
        user_id=draft.user_id,
        documents=_load_revision_documents(
            connection=connection,
            user_id=draft.user_id,
            node_id=draft.owner_node_id,
            revision_id=draft.base_revision_id,
        ),
    )
    if draft.documents != base_documents:
        raise MemoryCatalogIntegrityError(
            "Agent Experience repair must not delete or rewrite evidence text"
        )
    if any(link.state == "removed" for link in draft.links):
        raise MemoryCatalogIntegrityError(
            "Agent Experience repair must not publish removed references"
        )
    validate_agent_experience_evidence_links(
        connection=connection,
        draft=draft,
    )
    target_ids = tuple(sorted({link.target_fragment_id for link in draft.links}))
    placeholders = ",".join("?" for _ in target_ids)
    target_revision_ids = {
        str(row[0])
        for row in connection.execute(
            f"""SELECT revision_id FROM memory_fragments
                WHERE user_id = ? AND fragment_id IN ({placeholders})""",
            (draft.user_id, *target_ids),
        ).fetchall()
    }
    base_revision_ids = {
        str(row[0])
        for row in connection.execute(
            """SELECT source_revision_id FROM memory_evidence_edges
               WHERE user_id = ? AND derived_revision_id = ?
                 AND evidence_role = 'source_action'""",
            (draft.user_id, draft.base_revision_id),
        ).fetchall()
    }
    if target_revision_ids != base_revision_ids:
        raise MemoryCatalogIntegrityError(
            "Agent Experience repair evidence differs from its base revision"
        )


def _drop_deleted_action_evidence(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    documents: tuple[MemoryDocument, ...],
) -> tuple[MemoryDocument, ...]:
    """Drop the evidence of Actions deleted from history; it cannot be rebuilt."""

    live_actions = {
        str(row[0])
        for row in connection.execute(
            """SELECT source_record_id FROM memory_nodes
               WHERE user_id = ? AND source_type = 'action'""",
            (user_id,),
        )
    }
    return drop_deleted_references(
        documents,
        linked_ref_ids=frozenset(
            evidence.local_ref_id
            for document in documents
            if document.source_path != AGENT_EXPERIENCE_INDEX_PATH
            for evidence in parse_agent_experience_evidence(document.content)
            if evidence.action_id in live_actions
        ),
    )


def _load_revision_documents(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    node_id: str,
    revision_id: str,
) -> tuple[MemoryDocument, ...]:
    rows = connection.execute(
        """
        SELECT source_path, content_text FROM memory_fragments
        WHERE user_id = ? AND revision_id = ? AND block_kind = 'document_root'
          AND EXISTS (
              SELECT 1 FROM memory_revisions
              WHERE user_id = ? AND revision_id = ? AND node_id = ?
          )
        ORDER BY source_path
        """,
        (user_id, revision_id, user_id, revision_id, node_id),
    ).fetchall()
    if not rows:
        raise MemoryCatalogIntegrityError("Agent Experience repair base is absent")
    return tuple(
        MemoryDocument(str(row["source_path"]), str(row["content_text"]))
        for row in rows
    )


def _load_action_evidence_fragment(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    base_revision_id: str,
) -> str:
    rows = connection.execute(
        """
        SELECT revisions.revision_id, fragments.fragment_id
        FROM memory_evidence_edges AS edges
        JOIN memory_revisions AS revisions
          ON revisions.user_id = edges.user_id
         AND revisions.revision_id = edges.source_revision_id
        JOIN memory_nodes AS nodes
          ON nodes.user_id = revisions.user_id
         AND nodes.node_id = revisions.node_id
        JOIN memory_fragments AS fragments
          ON fragments.user_id = revisions.user_id
         AND fragments.revision_id = revisions.revision_id
        WHERE edges.user_id = ? AND edges.derived_revision_id = ?
          AND edges.evidence_role = 'source_action'
          AND nodes.source_type = 'action' AND nodes.source_record_id = ?
          AND nodes.lifecycle IN ('active', 'tombstoned')
          AND nodes.integrity = 'healthy' AND revisions.body_kind = 'inline'
          AND fragments.block_kind != 'record_root'
        ORDER BY revisions.revision_id,
                 CASE WHEN fragments.block_kind = 'document_root' THEN 1 ELSE 0 END,
                 fragments.source_path, fragments.block_index
        """,
        (user_id, base_revision_id, action_id),
    ).fetchall()
    revision_ids = {str(row["revision_id"]) for row in rows}
    if not rows:
        raise MemoryCatalogIntegrityError(
            "Agent Experience evidence Action cannot be reconstructed"
        )
    if len(revision_ids) != 1:
        raise MemoryCatalogIntegrityError(
            "Agent Experience evidence Action revision is ambiguous"
        )
    return str(rows[0]["fragment_id"])


def write_artifact_repair_projection(
    *,
    connection: sqlite3.Connection,
    revision: MemoryRevision,
    source: MemorySource,
    source_record_id: str,
) -> None:
    if revision.artifact_root_path is None:
        raise MemoryCatalogIntegrityError("artifact repair revision has no path")
    if source == "agent_experience":
        # Agent Experience is Catalog-native; its artifact head is the projection.
        return
    profile_brief = (revision.profile_brief or "").strip()
    if not profile_brief:
        profile_brief = _repair_profile_brief(
            connection=connection,
            user_id=revision.user_id,
            source=source,
            source_record_id=source_record_id,
        )
        revision_cursor = connection.execute(
            """
            UPDATE memory_revisions SET profile_brief = ?
            WHERE user_id = ? AND revision_id = ? AND profile_brief IS NULL
            """,
            (profile_brief, revision.user_id, revision.revision_id),
        )
        if revision_cursor.rowcount != 1:
            raise MemoryCatalogIntegrityError(
                "repair revision profile brief is not writable"
            )
    if source == "fact":
        cursor = connection.execute(
            """
            UPDATE agent_facts
            SET structured_fact_storage_path = ?, structured_fact_sha256 = ?,
                facts_profile_brief = ?, updated_at = ?
            WHERE user_id = ? AND fact_id = ? AND status = 'success'
            """,
            (
                f"{revision.artifact_root_path}/facts/index.md",
                revision.content_sha256,
                profile_brief,
                revision.created_at,
                revision.user_id,
                source_record_id,
            ),
        )
    elif source == "long_term_insight":
        cursor = connection.execute(
            """
            UPDATE agent_long_term_insight_state
            SET storage_path = ?, sha256 = ?, insight_profile_brief = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                f"{revision.artifact_root_path}/insights/index.md",
                revision.content_sha256,
                profile_brief,
                revision.created_at,
                revision.user_id,
            ),
        )
    else:
        raise MemoryCatalogIntegrityError("unsupported artifact repair source")
    if cursor.rowcount != 1:
        raise MemoryCatalogIntegrityError("artifact repair domain projection is absent")


def _repair_profile_brief(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    source: MemorySource,
    source_record_id: str,
) -> str:
    if source == "fact":
        row = connection.execute(
            """
            SELECT facts_profile_brief AS profile_brief FROM agent_facts
            WHERE user_id = ? AND fact_id = ? AND status = 'success'
            """,
            (user_id, source_record_id),
        ).fetchone()
    elif source == "long_term_insight":
        row = connection.execute(
            """
            SELECT insight_profile_brief AS profile_brief
            FROM agent_long_term_insight_state WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    else:
        raise MemoryCatalogIntegrityError("unsupported artifact repair source")
    if row is None or not str(row["profile_brief"] or "").strip():
        raise MemoryCatalogIntegrityError("artifact repair profile brief is absent")
    return str(row["profile_brief"]).strip()


def build_repair_payload(*, user_id: str, source_record_id: str) -> str:
    return json.dumps(
        {"user_id": user_id, "source_record_id": source_record_id},
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_repair_payload(payload_json: str) -> tuple[str, str]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise MemoryCatalogIntegrityError("artifact repair payload is invalid") from exc
    if not isinstance(payload, dict):
        raise MemoryCatalogIntegrityError("artifact repair payload must be an object")
    user_id = payload.get("user_id")
    source_record_id = payload.get("source_record_id")
    if not isinstance(user_id, str) or not user_id:
        raise MemoryCatalogIntegrityError("artifact repair user is invalid")
    if not isinstance(source_record_id, str) or not source_record_id:
        raise MemoryCatalogIntegrityError("artifact repair source is invalid")
    return user_id, source_record_id


def _repair_intent_kind(source: MemorySource) -> MemoryIntentKind:
    if source == "fact":
        return "fact_repair"
    if source == "long_term_insight":
        return "long_term_insight_repair"
    if source == "agent_experience":
        return "agent_experience_repair"
    raise ValueError("artifact repairs are unsupported for this memory source")

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .artifact_intent_payload import encode_artifact_intent_payload
from .artifact_manifest import build_artifact_manifest, manifest_sha256
from .artifact_paths import confined_artifact_path, memory_revision_relative_path
from .artifact_tree_durability import fsync_artifact_tree
from .connection import open_memory_catalog_connection
from .errors import (
    MemoryCatalogIntegrityError,
    MemoryLinkValidationError,
    MemoryPublicationConflictError,
    MemoryPublicationPendingIntentError,
)
from .fragments import (
    MEMORY_FRAGMENT_SCHEMA_VERSION,
    artifact_content_sha256,
    build_fragments,
    locate_memory_references,
)
from .models import (
    DraftLink,
    MemoryBodyKind,
    MemoryDraftCheckpoint,
    MemoryEvidenceEdge,
    MemoryFragment,
    MemoryIntentKind,
    MemoryRevision,
    MemorySource,
)
from .observability import observe_memory_publication
from .repository import (
    ensure_preparing_node,
    insert_fragments,
    insert_revision,
    require_node,
)

DomainProjectionWriter = Callable[[sqlite3.Connection, MemoryRevision], None]
type ArtifactPublicationRequestBuilder = Callable[
    [sqlite3.Connection], MemoryPublicationRequest
]


@dataclass(frozen=True, slots=True)
class MemoryPublicationRequest:
    source: MemorySource
    source_record_id: str
    draft: MemoryDraftCheckpoint
    body_kind: MemoryBodyKind
    artifact_root_path: str | None = None
    revision_id: str | None = None
    evidence_edges: tuple[MemoryEvidenceEdge, ...] = ()
    intent_kind: MemoryIntentKind | None = None
    domain_payload_json: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedArtifactRevision:
    request: MemoryPublicationRequest
    revision_id: str
    final_relative_path: str
    manifest_sha256: str
    manifest: str


def publish_artifact_revision(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    build_request: ArtifactPublicationRequestBuilder,
    write_domain_projection: DomainProjectionWriter,
) -> MemoryRevision:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            prepared = create_artifact_revision_intent(
                connection=connection,
                request=build_request(connection),
            )
    materialize_artifact_revision(artifact_root=artifact_root, prepared=prepared)
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            return activate_artifact_revision(
                connection=connection,
                prepared=prepared,
                write_domain_projection=write_domain_projection,
            )


def publish_inline_revision(
    *,
    connection: sqlite3.Connection,
    request: MemoryPublicationRequest,
    write_domain_projection: DomainProjectionWriter | None = None,
) -> MemoryRevision:
    if request.body_kind != "inline" or len(request.draft.documents) != 1:
        raise ValueError("inline publication requires exactly one document")
    revision = _build_revision(request=request, artifact_root_path=None)
    with _publication_observer(
        connection=connection, request=request, revision=revision
    ):
        _publish_in_transaction(
            connection=connection,
            request=request,
            revision=revision,
            write_domain_projection=write_domain_projection,
        )
    return revision


def create_artifact_revision_intent(
    *,
    connection: sqlite3.Connection,
    request: MemoryPublicationRequest,
) -> PreparedArtifactRevision:
    if request.body_kind != "artifact_tree":
        raise ValueError("artifact intent requires artifact_tree publication")
    if request.intent_kind is None or request.domain_payload_json is None:
        raise ValueError("artifact publication requires recoverable intent metadata")
    revision_id = request.revision_id or f"rev_{uuid.uuid4().hex}"
    relative_path = memory_revision_relative_path(
        user_id=request.draft.user_id,
        node_id=request.draft.owner_node_id,
        revision_id=revision_id,
    )
    manifest = build_artifact_manifest(
        revision_id=revision_id,
        source=request.source,
        source_record_id=request.source_record_id,
        draft=request.draft,
    )
    expected_manifest_sha256 = manifest_sha256(manifest)
    intent_payload_json = encode_artifact_intent_payload(
        domain_payload_json=request.domain_payload_json,
        draft=request.draft,
    )
    now = now_utc_iso()
    node = require_node(
        connection=connection,
        user_id=request.draft.user_id,
        node_id=request.draft.owner_node_id,
    )
    _validate_base(
        node_current_revision_id=node.current_revision_id, draft=request.draft
    )
    pending_intent = connection.execute(
        """
        SELECT revision_id
        FROM memory_revision_intents
        WHERE user_id = ? AND node_id = ?
        """,
        (request.draft.user_id, request.draft.owner_node_id),
    ).fetchone()
    if pending_intent is not None:
        raise MemoryPublicationPendingIntentError(
            "memory node already has a pending publication intent"
        )
    connection.execute(
        """
        INSERT INTO memory_revision_intents(
            user_id, revision_id, node_id, base_revision_id, manifest_sha256,
            artifact_root_path, intent_kind, domain_payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request.draft.user_id,
            revision_id,
            request.draft.owner_node_id,
            request.draft.base_revision_id,
            expected_manifest_sha256,
            relative_path,
            request.intent_kind,
            intent_payload_json,
            now,
        ),
    )
    return PreparedArtifactRevision(
        request, revision_id, relative_path, expected_manifest_sha256, manifest
    )


def materialize_artifact_revision(
    *, artifact_root: Path, prepared: PreparedArtifactRevision
) -> Path:
    final_path = confined_artifact_path(artifact_root, prepared.final_relative_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        raise FileExistsError(final_path)
    with tempfile.TemporaryDirectory(
        prefix="memory-revision-", dir=final_path.parent
    ) as temp:
        temporary_path = Path(temp)
        for document in prepared.request.draft.documents:
            target = confined_artifact_path(temporary_path, document.source_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_durable_text(target, document.content)
        _write_durable_text(temporary_path / "manifest.json", prepared.manifest)
        actual_manifest_hash = manifest_sha256(prepared.manifest)
        if actual_manifest_hash != prepared.manifest_sha256:
            raise MemoryCatalogIntegrityError(
                "artifact manifest changed after intent creation"
            )
        fsync_artifact_tree(tree_root=temporary_path, ancestor_root=artifact_root)
        os.replace(temporary_path, final_path)
        fsync_artifact_tree(tree_root=final_path, ancestor_root=artifact_root)
    return final_path


def activate_artifact_revision(
    *,
    connection: sqlite3.Connection,
    prepared: PreparedArtifactRevision,
    write_domain_projection: DomainProjectionWriter | None = None,
) -> MemoryRevision:
    row = connection.execute(
        """
        SELECT manifest_sha256, artifact_root_path
        FROM memory_revision_intents
        WHERE user_id = ? AND revision_id = ? AND node_id = ?
        """,
        (
            prepared.request.draft.user_id,
            prepared.revision_id,
            prepared.request.draft.owner_node_id,
        ),
    ).fetchone()
    if row is None or str(row["manifest_sha256"]) != prepared.manifest_sha256:
        raise MemoryCatalogIntegrityError(
            "artifact revision intent is absent or changed"
        )
    revision = _build_revision(
        request=prepared.request,
        artifact_root_path=str(row["artifact_root_path"]),
        revision_id=prepared.revision_id,
    )
    with _publication_observer(
        connection=connection, request=prepared.request, revision=revision
    ):
        _publish_in_transaction(
            connection=connection,
            request=prepared.request,
            revision=revision,
            write_domain_projection=write_domain_projection,
        )
    connection.execute(
        "DELETE FROM memory_revision_intents WHERE user_id = ? AND revision_id = ?",
        (revision.user_id, revision.revision_id),
    )
    return revision


def _publication_observer(
    *,
    connection: sqlite3.Connection,
    request: MemoryPublicationRequest,
    revision: MemoryRevision,
) -> AbstractContextManager[None]:
    return observe_memory_publication(
        connection=connection,
        user_id=revision.user_id,
        node_id=revision.node_id,
        revision_id=revision.revision_id,
        run_id=request.draft.draft_session_id,
        link_states=tuple(link.state for link in request.draft.links),
    )


def _publish_in_transaction(
    *,
    connection: sqlite3.Connection,
    request: MemoryPublicationRequest,
    revision: MemoryRevision,
    write_domain_projection: DomainProjectionWriter | None,
) -> None:
    node = require_node(
        connection=connection,
        user_id=revision.user_id,
        node_id=revision.node_id,
    )
    _validate_base(
        node_current_revision_id=node.current_revision_id, draft=request.draft
    )
    fragments = build_fragments(
        user_id=revision.user_id,
        revision_id=revision.revision_id,
        documents=request.draft.documents,
    )
    links = _validate_and_materialize_links(
        connection=connection,
        draft=request.draft,
        revision_id=revision.revision_id,
        fragments=fragments,
    )
    insert_revision(connection=connection, revision=revision)
    insert_fragments(connection=connection, fragments=fragments)
    for source_fragment_id, link in links:
        connection.execute(
            """
            INSERT INTO memory_links(
                user_id, source_revision_id, local_ref_id, source_fragment_id,
                target_fragment_id, reference_note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision.user_id,
                revision.revision_id,
                link.local_ref_id,
                source_fragment_id,
                link.target_fragment_id,
                link.reference_note,
                link.created_at,
            ),
        )
    if request.draft.base_revision_id is not None:
        connection.execute(
            """
            INSERT INTO memory_revision_parents(
                user_id, node_id, child_revision_id, parent_revision_id
            ) VALUES (?, ?, ?, ?)
            """,
            (
                revision.user_id,
                revision.node_id,
                revision.revision_id,
                request.draft.base_revision_id,
            ),
        )
    now = now_utc_iso()
    if request.draft.base_revision_id is None:
        cursor = connection.execute(
            """
            UPDATE memory_nodes
            SET lifecycle = 'active', current_revision_id = ?, updated_at = ?
            WHERE user_id = ? AND node_id = ? AND lifecycle = 'preparing'
              AND current_revision_id IS NULL
            """,
            (revision.revision_id, now, revision.user_id, revision.node_id),
        )
    else:
        cursor = connection.execute(
            """
            UPDATE memory_nodes
            SET current_revision_id = ?, updated_at = ?
            WHERE user_id = ? AND node_id = ? AND lifecycle = 'active'
              AND integrity = 'healthy' AND current_revision_id = ?
            """,
            (
                revision.revision_id,
                now,
                revision.user_id,
                revision.node_id,
                request.draft.base_revision_id,
            ),
        )
    if cursor.rowcount != 1:
        raise MemoryPublicationConflictError(
            "memory node head changed before activation"
        )
    for edge in request.evidence_edges:
        if (
            edge.user_id != revision.user_id
            or edge.derived_revision_id != revision.revision_id
        ):
            raise MemoryCatalogIntegrityError(
                "evidence edge belongs to another revision"
            )
        connection.execute(
            """
            INSERT INTO memory_evidence_edges(
                user_id, derived_revision_id, source_revision_id, evidence_role
            ) VALUES (?, ?, ?, ?)
            """,
            (
                edge.user_id,
                edge.derived_revision_id,
                edge.source_revision_id,
                edge.evidence_role,
            ),
        )
    if write_domain_projection is not None:
        write_domain_projection(connection, revision)


def allocate_publication_node(
    *, connection: sqlite3.Connection, request: MemoryPublicationRequest
) -> MemoryPublicationRequest:
    node = ensure_preparing_node(
        connection=connection,
        user_id=request.draft.user_id,
        source=request.source,
        source_record_id=request.source_record_id,
        node_id=request.draft.owner_node_id,
    )
    if node.node_id != request.draft.owner_node_id:
        raise MemoryPublicationConflictError("draft owner does not match catalog node")
    return request


def _build_revision(
    *,
    request: MemoryPublicationRequest,
    artifact_root_path: str | None,
    revision_id: str | None = None,
) -> MemoryRevision:
    selected_revision_id = (
        revision_id or request.revision_id or f"rev_{uuid.uuid4().hex}"
    )
    inline_body = (
        request.draft.documents[0].content if request.body_kind == "inline" else None
    )
    return MemoryRevision(
        user_id=request.draft.user_id,
        revision_id=selected_revision_id,
        node_id=request.draft.owner_node_id,
        body_kind=request.body_kind,
        inline_body=inline_body,
        artifact_root_path=artifact_root_path,
        fragment_schema_version=MEMORY_FRAGMENT_SCHEMA_VERSION,
        content_sha256=artifact_content_sha256(request.draft.documents),
        profile_brief=None,
        created_at=now_utc_iso(),
    )


def _validate_and_materialize_links(
    *,
    connection: sqlite3.Connection,
    draft: MemoryDraftCheckpoint,
    revision_id: str,
    fragments: tuple[MemoryFragment, ...],
) -> tuple[tuple[str, DraftLink], ...]:
    active_links = tuple(link for link in draft.links if link.state != "removed")
    occurrences: dict[str, tuple[str, str | None, int]] = {}
    for located in locate_memory_references(draft.documents):
        if located.local_ref_id in occurrences:
            raise MemoryLinkValidationError("local ref IDs must be revision-unique")
        if located.block_index is None:
            raise MemoryLinkValidationError("ref tag has no containing source block")
        occurrences[located.local_ref_id] = (
            located.source_path,
            located.reference_note,
            located.block_index,
        )
    if set(occurrences) != {link.local_ref_id for link in active_links}:
        raise MemoryLinkValidationError(
            "published tags and link mappings are not one-to-one"
        )
    fragments_by_locator = {
        (fragment.source_path, fragment.block_index): fragment for fragment in fragments
    }
    materialized: list[tuple[str, DraftLink]] = []
    for link in active_links:
        source_path, note, block_index = occurrences[link.local_ref_id]
        if note != link.reference_note:
            raise MemoryLinkValidationError("tag note and link metadata differ")
        source_fragment = fragments_by_locator.get((source_path, block_index))
        if source_fragment is None:
            raise MemoryLinkValidationError("source fragment is absent")
        target = connection.execute(
            """
            SELECT nodes.lifecycle, nodes.integrity
            FROM memory_fragments AS fragments
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id
             AND revisions.revision_id = fragments.revision_id
            JOIN memory_nodes AS nodes
              ON nodes.user_id = revisions.user_id AND nodes.node_id = revisions.node_id
            WHERE fragments.user_id = ? AND fragments.fragment_id = ?
            """,
            (draft.user_id, link.target_fragment_id),
        ).fetchone()
        if target is None:
            raise MemoryLinkValidationError("target fragment is absent")
        if link.state == "pending" and (
            str(target["lifecycle"]) != "active"
            or str(target["integrity"]) != "healthy"
        ):
            raise MemoryLinkValidationError(
                "new links require an active healthy target"
            )
        materialized.append((source_fragment.fragment_id, link))
    return tuple(materialized)


def _validate_base(
    *, node_current_revision_id: str | None, draft: MemoryDraftCheckpoint
) -> None:
    if node_current_revision_id != draft.base_revision_id:
        raise MemoryPublicationConflictError(
            "draft base is not the current node revision"
        )


def _write_durable_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())

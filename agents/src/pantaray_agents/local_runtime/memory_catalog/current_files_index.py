"""Index ordinary memory files after the reader/writer/repair cutover.

The caller holds the memory directory lock and a write transaction through commit.
Migration must copy current documents and retire immutable repair before use.
The existing revision row is an index identity, not a new saved file generation.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections import Counter
from dataclasses import replace

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import (
    SQLiteTransactionOwnershipError,
)

from .editable_files import (
    EDITABLE_MEMORY_ROOTS,
    EditableMemorySource,
    MemoryFiles,
    editable_memory_relative_path,
)
from .errors import MemoryCatalogIntegrityError
from .fragments import (
    MEMORY_FRAGMENT_SCHEMA_VERSION,
    artifact_content_sha256,
    build_fragments,
    locate_memory_references,
)
from .models import MemoryDocument, MemoryFragment, MemoryNode, MemoryRevision
from .repository import (
    ensure_preparing_node,
    insert_fragments,
    insert_revision,
    list_revision_fragments,
    list_revision_links,
    load_revision,
    require_node,
)


def refresh_current_memory_index(
    *, connection: sqlite3.Connection, files: MemoryFiles
) -> None:
    if not connection.in_transaction:
        raise SQLiteTransactionOwnershipError(
            "current memory indexing requires the caller's write transaction"
        )
    documents = files.documents()
    for root, source in EDITABLE_MEMORY_ROOTS.items():
        _refresh_category(
            connection=connection,
            user_id=files.user_id,
            source=source,
            documents=tuple(
                item for item in documents if item.source_path.startswith(f"{root}/")
            ),
        )


def _current_node(
    *, connection: sqlite3.Connection, user_id: str, source: EditableMemorySource
) -> MemoryNode:
    # Do not choose an older healthy record when the newest active one is corrupt.
    row = connection.execute(
        """
        SELECT node_id FROM memory_nodes
        WHERE user_id = ? AND source_type = ? AND lifecycle = 'active'
        ORDER BY updated_at DESC, source_record_id DESC, node_id DESC LIMIT 1
        """,
        (user_id, source),
    ).fetchone()
    node = (
        require_node(connection=connection, user_id=user_id, node_id=str(row[0]))
        if row is not None
        else ensure_preparing_node(
            connection=connection,
            user_id=user_id,
            source=source,
            source_record_id=user_id,
        )
    )
    if node.integrity != "healthy" or node.lifecycle == "tombstoned":
        raise MemoryCatalogIntegrityError("current memory index owner is not editable")
    return node


def _refresh_category(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    source: EditableMemorySource,
    documents: tuple[MemoryDocument, ...],
) -> None:
    node = _current_node(connection=connection, user_id=user_id, source=source)
    # Current links own provenance and already protect their targets from GC.
    # Successful-Action edges belonged to the retired publication format.
    connection.execute(
        "DELETE FROM memory_evidence_edges WHERE user_id = ? AND derived_revision_id = ?",
        (user_id, node.current_revision_id),
    )
    relative_root = editable_memory_relative_path(user_id)
    digest = artifact_content_sha256(documents)
    now = now_utc_iso()
    if node.current_revision_id is None:
        revision = MemoryRevision(
            user_id=user_id,
            revision_id=f"rev_{uuid.uuid4().hex}",
            node_id=node.node_id,
            body_kind="artifact_tree",
            inline_body=None,
            artifact_root_path=relative_root,
            fragment_schema_version=MEMORY_FRAGMENT_SCHEMA_VERSION,
            content_sha256=digest,
            profile_brief=None,
            created_at=now,
        )
        insert_revision(connection=connection, revision=revision)
    else:
        loaded = load_revision(
            connection=connection,
            user_id=user_id,
            revision_id=node.current_revision_id,
        )
        if loaded is None:
            raise MemoryCatalogIntegrityError(
                "current memory index revision is missing"
            )
        revision = loaded
        if (
            revision.content_sha256 == digest
            and revision.artifact_root_path == relative_root
            and revision.fragment_schema_version == MEMORY_FRAGMENT_SCHEMA_VERSION
        ):
            _prune_unwritten_links(
                connection=connection, revision=revision, documents=documents
            )
            return
        connection.execute(
            """
            UPDATE memory_revisions
            SET body_kind = 'artifact_tree', inline_body = NULL,
                artifact_root_path = ?, fragment_schema_version = ?,
                profile_brief = CASE WHEN content_sha256 = ? THEN profile_brief ELSE NULL END,
                content_sha256 = ?, created_at = ?
            WHERE user_id = ? AND revision_id = ?
            """,
            (
                relative_root,
                MEMORY_FRAGMENT_SCHEMA_VERSION,
                digest,
                digest,
                now,
                user_id,
                revision.revision_id,
            ),
        )
    fragments = tuple(
        replace(item, fragment_id=_document_root_id(item))
        if item.block_kind == "document_root"
        else item
        for item in build_fragments(
            user_id=user_id, revision_id=revision.revision_id, documents=documents
        )
    )
    _replace_projection(
        connection=connection,
        revision=revision,
        documents=documents,
        fragments=fragments,
    )
    connection.execute(
        """
        UPDATE memory_nodes
        SET lifecycle = 'active', current_revision_id = ?, updated_at = ?
        WHERE user_id = ? AND node_id = ?
        """,
        (revision.revision_id, now, user_id, node.node_id),
    )


def _document_root_id(fragment: MemoryFragment) -> str:
    identity = "\0".join((fragment.user_id, fragment.revision_id, fragment.source_path))
    return f"frag_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"


def _prune_unwritten_links(
    *,
    connection: sqlite3.Connection,
    revision: MemoryRevision,
    documents: tuple[MemoryDocument, ...],
) -> None:
    # Link creation commits its mapping before writing the tag. A stopped writer
    # can leave an unused mapping even when the indexed file digest is unchanged.
    occurrences = tuple(
        ref
        for document in documents
        for ref in extract_markdown_references(document.content)
    )
    counts = Counter(ref.local_ref_id for ref in occurrences)
    notes = {ref.local_ref_id: ref.note for ref in occurrences}
    connection.executemany(
        "DELETE FROM memory_links WHERE user_id = ? AND source_revision_id = ? AND local_ref_id = ?",
        (
            (revision.user_id, revision.revision_id, link.local_ref_id)
            for link in list_revision_links(
                connection=connection,
                user_id=revision.user_id,
                revision_id=revision.revision_id,
            )
            if counts[link.local_ref_id] != 1
            or notes[link.local_ref_id] != link.reference_note
        ),
    )


def _replace_projection(
    *,
    connection: sqlite3.Connection,
    revision: MemoryRevision,
    documents: tuple[MemoryDocument, ...],
    fragments: tuple[MemoryFragment, ...],
) -> None:
    user_id = revision.user_id
    existing = {
        item.fragment_id: item
        for item in list_revision_fragments(
            connection=connection, user_id=user_id, revision_id=revision.revision_id
        )
    }
    current = {item.fragment_id: item for item in fragments}
    roots = {
        item.source_path: item.fragment_id
        for item in fragments
        if item.block_kind == "document_root"
    }
    retained = {
        identity
        for identity, item in existing.items()
        if identity in current
        and current[identity].content_sha256 == item.content_sha256
    }
    replaced = tuple(
        item for identity, item in existing.items() if identity not in retained
    )
    connection.executemany(
        "UPDATE memory_fragments SET block_kind = ?, heading_path = ? WHERE user_id = ? AND fragment_id = ?",
        (
            (item.block_kind, item.heading_path, user_id, item.fragment_id)
            for item in fragments
            if item.fragment_id in retained and item != existing[item.fragment_id]
        ),
    )
    for item in replaced:
        target = roots.get(item.source_path)
        if target is None:
            connection.execute(
                "DELETE FROM memory_links WHERE user_id = ? AND target_fragment_id = ?",
                (user_id, item.fragment_id),
            )
        else:
            connection.execute(
                "UPDATE memory_links SET target_fragment_id = ? WHERE user_id = ? AND target_fragment_id = ?",
                (target, user_id, item.fragment_id),
            )
    # Capture outgoing mappings after their targets have been rebound above.
    links = {
        link.local_ref_id: link
        for link in list_revision_links(
            connection=connection, user_id=user_id, revision_id=revision.revision_id
        )
    }
    connection.execute(
        "DELETE FROM memory_links WHERE user_id = ? AND source_revision_id = ?",
        (user_id, revision.revision_id),
    )
    connection.executemany(
        "DELETE FROM memory_fragments WHERE user_id = ? AND fragment_id = ?",
        ((user_id, item.fragment_id) for item in replaced),
    )
    # DELETE/INSERT drives the existing FTS and embedding-work triggers. Unchanged
    # fragments retain their embeddings; no network call runs under the file lock.
    insert_fragments(
        connection=connection,
        fragments=tuple(
            item for identity, item in current.items() if identity not in retained
        ),
    )
    locations = locate_memory_references(documents)
    counts = Counter(item.local_ref_id for item in locations)
    by_locator = {(item.source_path, item.block_index): item for item in fragments}
    for location in locations:
        link = links.get(location.local_ref_id)
        if (
            link is None
            or counts[location.local_ref_id] != 1
            or location.block_index is None
            or location.reference_note != link.reference_note
        ):
            continue
        source = by_locator[location.source_path, location.block_index]
        connection.execute(
            """
            INSERT INTO memory_links(
                user_id, source_revision_id, local_ref_id, source_fragment_id,
                target_fragment_id, reference_note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                revision.revision_id,
                link.local_ref_id,
                source.fragment_id,
                link.target_fragment_id,
                link.reference_note,
                link.created_at,
            ),
        )

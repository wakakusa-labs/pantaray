from __future__ import annotations

import sqlite3
import uuid

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .artifact_intent_payload import decode_artifact_intent_payload
from .artifact_manifest import validate_intent_kind
from .errors import MemoryCatalogIntegrityError
from .models import (
    ArtifactIntent,
    MemoryFragment,
    MemoryLink,
    MemoryNode,
    MemoryRevision,
    MemorySource,
)


def list_artifact_revision_intents(
    connection: sqlite3.Connection,
) -> tuple[ArtifactIntent, ...]:
    rows = connection.execute(
        """
        SELECT user_id, revision_id, node_id, base_revision_id, manifest_sha256,
               artifact_root_path, intent_kind, domain_payload_json
        FROM memory_revision_intents ORDER BY created_at, revision_id
        """
    ).fetchall()
    return tuple(_intent_from_row(row) for row in rows)


def load_artifact_revision_intent_for_node(
    *, connection: sqlite3.Connection, user_id: str, node_id: str
) -> ArtifactIntent | None:
    row = connection.execute(
        """
        SELECT user_id, revision_id, node_id, base_revision_id, manifest_sha256,
               artifact_root_path, intent_kind, domain_payload_json
        FROM memory_revision_intents
        WHERE user_id = ? AND node_id = ?
        """,
        (user_id, node_id),
    ).fetchone()
    return _intent_from_row(row) if row is not None else None


def ensure_preparing_node(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    source: MemorySource,
    source_record_id: str,
    node_id: str | None = None,
) -> MemoryNode:
    existing = load_node_by_source(
        connection=connection,
        user_id=user_id,
        source=source,
        source_record_id=source_record_id,
    )
    if existing is not None:
        return existing
    now = now_utc_iso()
    allocated = node_id or f"mem_{uuid.uuid4().hex}"
    connection.execute(
        """
        INSERT INTO memory_nodes(
            user_id, node_id, source_type, source_record_id, lifecycle,
            integrity, current_revision_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'preparing', 'healthy', NULL, ?, ?)
        """,
        (user_id, allocated, source, source_record_id, now, now),
    )
    return MemoryNode(
        user_id=user_id,
        node_id=allocated,
        source=source,
        source_record_id=source_record_id,
        lifecycle="preparing",
        integrity="healthy",
        current_revision_id=None,
    )


def load_node_by_source(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    source: MemorySource,
    source_record_id: str,
) -> MemoryNode | None:
    row = connection.execute(
        """
        SELECT user_id, node_id, source_type, source_record_id, lifecycle,
               integrity, current_revision_id
        FROM memory_nodes
        WHERE user_id = ? AND source_type = ? AND source_record_id = ?
        """,
        (user_id, source, source_record_id),
    ).fetchone()
    return _node_from_row(row) if row is not None else None


def load_latest_active_node_by_source(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    source: MemorySource,
) -> MemoryNode | None:
    row = connection.execute(
        """
        SELECT user_id, node_id, source_type, source_record_id, lifecycle,
               integrity, current_revision_id
        FROM memory_nodes
        WHERE user_id = ? AND source_type = ?
          AND lifecycle = 'active'
          AND integrity = 'healthy'
          AND current_revision_id IS NOT NULL
        ORDER BY updated_at DESC, source_record_id DESC, node_id DESC
        LIMIT 1
        """,
        (user_id, source),
    ).fetchone()
    return _node_from_row(row) if row is not None else None


def load_node(
    *, connection: sqlite3.Connection, user_id: str, node_id: str
) -> MemoryNode | None:
    row = connection.execute(
        """
        SELECT user_id, node_id, source_type, source_record_id, lifecycle,
               integrity, current_revision_id
        FROM memory_nodes WHERE user_id = ? AND node_id = ?
        """,
        (user_id, node_id),
    ).fetchone()
    return _node_from_row(row) if row is not None else None


def load_revision(
    *, connection: sqlite3.Connection, user_id: str, revision_id: str
) -> MemoryRevision | None:
    row = connection.execute(
        """
        SELECT user_id, revision_id, node_id, body_kind, inline_body,
               artifact_root_path, fragment_schema_version, content_sha256,
               profile_brief, created_at
        FROM memory_revisions WHERE user_id = ? AND revision_id = ?
        """,
        (user_id, revision_id),
    ).fetchone()
    if row is None:
        return None
    return MemoryRevision(
        user_id=str(row["user_id"]),
        revision_id=str(row["revision_id"]),
        node_id=str(row["node_id"]),
        body_kind=str(row["body_kind"]),  # type: ignore[arg-type]
        inline_body=str(row["inline_body"]) if row["inline_body"] is not None else None,
        artifact_root_path=(
            str(row["artifact_root_path"])
            if row["artifact_root_path"] is not None
            else None
        ),
        fragment_schema_version=int(row["fragment_schema_version"]),
        content_sha256=str(row["content_sha256"]),
        profile_brief=(
            str(row["profile_brief"]) if row["profile_brief"] is not None else None
        ),
        created_at=str(row["created_at"]),
    )


def list_revision_fragments(
    *, connection: sqlite3.Connection, user_id: str, revision_id: str
) -> tuple[MemoryFragment, ...]:
    rows = connection.execute(
        """
        SELECT user_id, fragment_id, revision_id, source_path, block_kind,
               block_index, heading_path, content_text, content_sha256
        FROM memory_fragments
        WHERE user_id = ? AND revision_id = ?
        ORDER BY source_path, block_index
        """,
        (user_id, revision_id),
    ).fetchall()
    return tuple(_fragment_from_row(row) for row in rows)


def list_revision_links(
    *, connection: sqlite3.Connection, user_id: str, revision_id: str
) -> tuple[MemoryLink, ...]:
    rows = connection.execute(
        """
        SELECT user_id, source_revision_id, local_ref_id, source_fragment_id,
               target_fragment_id, reference_note, created_at
        FROM memory_links
        WHERE user_id = ? AND source_revision_id = ?
        ORDER BY local_ref_id
        """,
        (user_id, revision_id),
    ).fetchall()
    return tuple(
        MemoryLink(
            user_id=str(row["user_id"]),
            source_revision_id=str(row["source_revision_id"]),
            local_ref_id=str(row["local_ref_id"]),
            source_fragment_id=str(row["source_fragment_id"]),
            target_fragment_id=str(row["target_fragment_id"]),
            reference_note=str(row["reference_note"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    )


def require_node(
    *, connection: sqlite3.Connection, user_id: str, node_id: str
) -> MemoryNode:
    node = load_node(connection=connection, user_id=user_id, node_id=node_id)
    if node is None:
        raise MemoryCatalogIntegrityError("memory node does not exist")
    return node


def _node_from_row(row: sqlite3.Row) -> MemoryNode:
    return MemoryNode(
        user_id=str(row["user_id"]),
        node_id=str(row["node_id"]),
        source=str(row["source_type"]),  # type: ignore[arg-type]
        source_record_id=str(row["source_record_id"]),
        lifecycle=str(row["lifecycle"]),  # type: ignore[arg-type]
        integrity=str(row["integrity"]),  # type: ignore[arg-type]
        current_revision_id=(
            str(row["current_revision_id"])
            if row["current_revision_id"] is not None
            else None
        ),
    )


def _intent_from_row(row: sqlite3.Row) -> ArtifactIntent:
    user_id = str(row["user_id"])
    node_id = str(row["node_id"])
    base_revision_id = (
        str(row["base_revision_id"]) if row["base_revision_id"] is not None else None
    )
    domain_payload_json, draft = decode_artifact_intent_payload(
        payload_json=str(row["domain_payload_json"]),
        expected_user_id=user_id,
        expected_node_id=node_id,
        expected_base_revision_id=base_revision_id,
    )
    return ArtifactIntent(
        user_id=user_id,
        revision_id=str(row["revision_id"]),
        node_id=node_id,
        base_revision_id=base_revision_id,
        manifest_sha256=str(row["manifest_sha256"]),
        artifact_root_path=str(row["artifact_root_path"]),
        intent_kind=validate_intent_kind(str(row["intent_kind"])),
        domain_payload_json=domain_payload_json,
        draft=draft,
    )


def _fragment_from_row(row: sqlite3.Row) -> MemoryFragment:
    return MemoryFragment(
        user_id=str(row["user_id"]),
        fragment_id=str(row["fragment_id"]),
        revision_id=str(row["revision_id"]),
        source_path=str(row["source_path"]),
        block_kind=str(row["block_kind"]),  # type: ignore[arg-type]
        block_index=int(row["block_index"]),
        heading_path=str(row["heading_path"])
        if row["heading_path"] is not None
        else None,
        content_text=str(row["content_text"]),
        content_sha256=str(row["content_sha256"]),
    )


def insert_revision(
    *, connection: sqlite3.Connection, revision: MemoryRevision
) -> None:
    connection.execute(
        """
        INSERT INTO memory_revisions(
            user_id, revision_id, node_id, body_kind, inline_body,
            artifact_root_path, fragment_schema_version, content_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision.user_id,
            revision.revision_id,
            revision.node_id,
            revision.body_kind,
            revision.inline_body,
            revision.artifact_root_path,
            revision.fragment_schema_version,
            revision.content_sha256,
            revision.created_at,
        ),
    )


def insert_fragments(
    *, connection: sqlite3.Connection, fragments: tuple[MemoryFragment, ...]
) -> None:
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

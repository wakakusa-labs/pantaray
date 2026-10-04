from __future__ import annotations

import shutil
import sqlite3
import uuid
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import register_after_commit

from .errors import MemoryCatalogIntegrityError, MemoryPublicationConflictError
from .observability import emit_memory_catalog_event


def archive_memory(
    *, connection: sqlite3.Connection, user_id: str, node_id: str
) -> None:
    now = now_utc_iso()
    cursor = connection.execute(
        """
        UPDATE memory_nodes
        SET lifecycle = 'tombstoned', updated_at = ?
        WHERE user_id = ? AND node_id = ? AND lifecycle = 'active'
        """,
        (now, user_id, node_id),
    )
    if cursor.rowcount != 1:
        raise MemoryPublicationConflictError("only an active memory can be archived")
    register_after_commit(
        connection=connection,
        callback=lambda: emit_memory_catalog_event(
            "memory_node_tombstoned", user_id=user_id, node_id=node_id
        ),
    )


def mark_memory_corrupt(
    *, connection: sqlite3.Connection, user_id: str, node_id: str
) -> None:
    cursor = connection.execute(
        """
        UPDATE memory_nodes SET integrity = 'corrupt', updated_at = ?
        WHERE user_id = ? AND node_id = ? AND lifecycle != 'preparing'
        """,
        (now_utc_iso(), user_id, node_id),
    )
    if cursor.rowcount != 1:
        raise MemoryCatalogIntegrityError("corrupt node is absent or still preparing")


def plan_revision_garbage_collection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    revision_id: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT revisions.artifact_root_path
        FROM memory_revisions AS revisions
        JOIN memory_nodes AS nodes
          ON nodes.user_id = revisions.user_id AND nodes.node_id = revisions.node_id
        WHERE revisions.user_id = ? AND revisions.revision_id = ?
          AND nodes.current_revision_id != revisions.revision_id
          AND revisions.artifact_root_path IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM memory_links AS links
              JOIN memory_fragments AS fragments
                ON fragments.user_id = links.user_id
               AND fragments.fragment_id = links.target_fragment_id
              WHERE fragments.user_id = revisions.user_id
                AND fragments.revision_id = revisions.revision_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM memory_evidence_edges AS edges
              WHERE edges.user_id = revisions.user_id
                AND (edges.source_revision_id = revisions.revision_id
                     OR edges.derived_revision_id = revisions.revision_id)
          )
          AND NOT EXISTS (
              SELECT 1 FROM memory_revision_intents AS intents
              WHERE intents.user_id = revisions.user_id
                AND (intents.revision_id = revisions.revision_id
                     OR intents.base_revision_id = revisions.revision_id)
          )
          AND NOT EXISTS (
              SELECT 1
              FROM workspace_manifest_roots AS roots
              JOIN workspace_manifests AS manifests
                ON manifests.manifest_id = roots.manifest_id
              JOIN agent_actions AS actions
                ON actions.user_id = manifests.user_id
               AND actions.action_id = manifests.action_id
              WHERE roots.source_type = 'agent_experience'
                AND roots.source_id = revisions.revision_id
                AND manifests.user_id = revisions.user_id
                AND actions.status = 'processing'
          )
        """,
        (user_id, revision_id),
    ).fetchone()
    if row is None:
        return None
    deletion_id = f"del_{uuid.uuid4().hex}"
    now = now_utc_iso()
    connection.execute(
        """
        INSERT INTO memory_artifact_deletions(
            user_id, deletion_id, artifact_path, reason, state, created_at, updated_at
        ) VALUES (?, ?, ?, 'revision_gc', 'planned', ?, ?)
        """,
        (user_id, deletion_id, str(row["artifact_root_path"]), now, now),
    )
    connection.execute(
        "DELETE FROM memory_revisions WHERE user_id = ? AND revision_id = ?",
        (user_id, revision_id),
    )
    return deletion_id


def process_artifact_deletion(
    *,
    connection: sqlite3.Connection,
    artifact_root: Path,
    deletion_id: str,
    user_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT artifact_path, state FROM memory_artifact_deletions
        WHERE user_id = ? AND deletion_id = ?
        """,
        (user_id, deletion_id),
    ).fetchone()
    if row is None:
        return
    path = _confined_child(artifact_root, str(row["artifact_path"]))
    if path.exists():
        shutil.rmtree(path)
    connection.execute(
        "DELETE FROM memory_artifact_deletions WHERE user_id = ? AND deletion_id = ?",
        (user_id, deletion_id),
    )


def process_pending_artifact_deletions(
    *, connection: sqlite3.Connection, artifact_root: Path
) -> int:
    rows = connection.execute(
        """
        SELECT user_id, deletion_id
        FROM memory_artifact_deletions
        WHERE reason = 'revision_gc'
        ORDER BY created_at, deletion_id
        """
    ).fetchall()
    for row in rows:
        user_id = str(row["user_id"])
        deletion_id = str(row["deletion_id"])
        emit_memory_catalog_event(
            "memory_artifact_deletion_retried",
            user_id=user_id,
            deletion_id=deletion_id,
        )
        try:
            process_artifact_deletion(
                connection=connection,
                artifact_root=artifact_root,
                user_id=user_id,
                deletion_id=deletion_id,
            )
        except OSError as exc:
            emit_memory_catalog_event(
                "memory_artifact_deletion_failed",
                user_id=user_id,
                deletion_id=deletion_id,
                fault_code=type(exc).__name__,
            )
            raise
    return len(rows)


def remove_abandoned_preparing_nodes(*, connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        """
        DELETE FROM memory_nodes
        WHERE lifecycle = 'preparing' AND current_revision_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM memory_revision_intents AS intents
              WHERE intents.user_id = memory_nodes.user_id
                AND intents.node_id = memory_nodes.node_id
          )
          AND (
              -- One unified Memory run owns all three memory categories, so a
              -- run the restart re-queued keeps the nodes it prepared.
              (source_type IN ('fact', 'long_term_insight', 'agent_experience')
               AND NOT EXISTS (
                  SELECT 1 FROM jobs AS jobs
                  WHERE jobs.user_id = memory_nodes.user_id
                    AND jobs.job_type = 'memory_update'
                    AND jobs.status IN (
                        'queued', 'running', 'paused', 'retryable_error'
                    )
              ))
              OR
              (source_type = 'action' AND NOT EXISTS (
                  SELECT 1 FROM agent_actions AS actions
                  WHERE actions.user_id = memory_nodes.user_id
                    AND actions.action_id = memory_nodes.source_record_id
                    AND actions.status = 'processing'
              ))
          )
        """
    )
    return cursor.rowcount


def _confined_child(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (root / relative_path).resolve()
    if resolved_root == candidate or resolved_root not in candidate.parents:
        raise MemoryCatalogIntegrityError("artifact deletion path escapes runtime root")
    return candidate

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.agents.workspace_context import (
    workspace_context_catalog_to_snapshot,
)
from pantaray_agents.local_runtime.artifacts.paths import resolve_artifact_path

from ...storage.migrations import MigrationError
from .common import _configure_connection, _serialize_json
from .workspace_context import build_workspace_context_catalog
from .workspace_settings import load_workspace_settings_in_connection

AGENT_EXPERIENCE_DIRNAME = "agent_experience"
AGENT_EXPERIENCE_INDEX_FILENAME = "index.md"


@dataclass(frozen=True, slots=True)
class AgentExperienceRoot:
    revision_id: str
    path: Path


def ensure_action_workspace_manifest(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    execution_session_id: str,
    workspace_path: Path,
    tool_results_path: Path,
    artifact_root: Path,
    created_at: str,
) -> str:
    manifest_id = f"manifest:{action_id}"
    scratch_root_id = f"root:{action_id}:scratch"
    scratch_root_path = workspace_path.resolve()
    scratch_real_path = str(scratch_root_path)
    tool_results_real_path = str(tool_results_path.resolve())
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        with connection:
            agent_experience_root = _prepare_agent_experience_root(
                connection=connection,
                user_id=user_id,
                action_id=action_id,
                artifact_root=artifact_root,
            )
            return ensure_action_workspace_manifest_in_connection(
                connection=connection,
                user_id=user_id,
                action_id=action_id,
                execution_session_id=execution_session_id,
                scratch_root_id=scratch_root_id,
                manifest_id=manifest_id,
                scratch_real_path=scratch_real_path,
                tool_results_real_path=tool_results_real_path,
                agent_experience_root=agent_experience_root,
                created_at=created_at,
            )


def ensure_action_workspace_manifest_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    execution_session_id: str,
    scratch_root_id: str,
    manifest_id: str,
    scratch_real_path: str,
    tool_results_real_path: str,
    agent_experience_root: AgentExperienceRoot | None,
    created_at: str,
) -> str:
    existing_manifest = connection.execute(
        "SELECT manifest_id FROM workspace_manifests WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    if existing_manifest is not None:
        existing_manifest_id = str(existing_manifest["manifest_id"])
        connection.execute(
            "UPDATE workspace_manifests SET execution_session_id = ? WHERE action_id = ?",
            (execution_session_id, action_id),
        )
        _upsert_tool_results_root(
            connection=connection,
            manifest_id=existing_manifest_id,
            action_id=action_id,
            real_path=tool_results_real_path,
            created_at=created_at,
        )
        # Each new run of an existing Action picks up folders the user registered
        # or removed since the previous run; a running session keeps its roots.
        connection.execute(
            """
            UPDATE workspace_manifests
            SET workspace_context_snapshot_json = ?
            WHERE manifest_id = ?
            """,
            (
                _workspace_context_snapshot_json(
                    connection=connection, user_id=user_id
                ),
                existing_manifest_id,
            ),
        )
        _reconcile_folder_roots(
            connection=connection,
            user_id=user_id,
            manifest_id=existing_manifest_id,
            created_at=created_at,
        )
        return existing_manifest_id
    connection.execute(
        """
        INSERT INTO workspace_manifests(
            manifest_id, user_id, action_id, execution_session_id,
            scratch_root_path, workspace_context_snapshot_json,
            created_at, materialized_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready')
        """,
        (
            manifest_id,
            user_id,
            action_id,
            execution_session_id,
            scratch_real_path,
            _workspace_context_snapshot_json(connection=connection, user_id=user_id),
            created_at,
            created_at,
        ),
    )
    _upsert_tool_results_root(
        connection=connection,
        manifest_id=manifest_id,
        action_id=action_id,
        real_path=tool_results_real_path,
        created_at=created_at,
    )
    connection.execute(
        """
        INSERT INTO workspace_manifest_roots(
            root_id, manifest_id, source_type, source_id, display_name,
            canonical_real_path, real_path, can_read, can_apply_patch,
            can_process_read, can_process_write, created_at
        ) VALUES (?, ?, 'scratch', ?, 'scratch', ?, ?, 1, 1, 1, 1, ?)
        """,
        (
            scratch_root_id,
            manifest_id,
            manifest_id,
            scratch_real_path,
            scratch_real_path,
            created_at,
        ),
    )
    _reconcile_folder_roots(
        connection=connection,
        user_id=user_id,
        manifest_id=manifest_id,
        created_at=created_at,
    )
    if agent_experience_root is not None:
        _insert_agent_experience_root(
            connection=connection,
            manifest_id=manifest_id,
            root=agent_experience_root,
            created_at=created_at,
        )
    return manifest_id


def _prepare_agent_experience_root(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    action_id: str,
    artifact_root: Path,
) -> AgentExperienceRoot | None:
    existing = connection.execute(
        "SELECT 1 FROM workspace_manifests WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    if existing is not None:
        return None
    return _resolve_agent_experience_root(
        connection=connection,
        user_id=user_id,
        artifact_root=artifact_root,
    )


def _upsert_tool_results_root(
    *,
    connection: sqlite3.Connection,
    manifest_id: str,
    action_id: str,
    real_path: str,
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO workspace_manifest_roots(
            root_id,
            manifest_id,
            source_type,
            source_id,
            display_name,
            canonical_real_path,
            real_path,
            can_read,
            can_apply_patch,
            can_process_read,
            can_process_write,
            created_at
        ) VALUES (?, ?, 'scratch', ?, 'tool results', ?, ?, 1, 0, 1, 0, ?)
        ON CONFLICT(root_id) DO UPDATE SET
            manifest_id = excluded.manifest_id,
            source_type = excluded.source_type,
            source_id = excluded.source_id,
            display_name = excluded.display_name,
            canonical_real_path = excluded.canonical_real_path,
            real_path = excluded.real_path,
            can_read = excluded.can_read,
            can_apply_patch = excluded.can_apply_patch,
            can_process_read = excluded.can_process_read,
            can_process_write = excluded.can_process_write
        """,
        (
            f"root:{action_id}:tool-results",
            manifest_id,
            manifest_id,
            real_path,
            real_path,
            created_at,
        ),
    )


def load_action_manifest_id(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    action_id: str,
) -> str:
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        row = connection.execute(
            """
            SELECT manifest_id
            FROM workspace_manifests
            WHERE action_id = ? AND status = 'ready'
            """,
            (action_id,),
        ).fetchone()
    if row is None:
        raise MigrationError(f"workspace manifest not found for action: {action_id}")
    return str(row["manifest_id"])


def _workspace_context_snapshot_json(
    *,
    connection: sqlite3.Connection,
    user_id: str,
) -> str:
    return _serialize_json(
        workspace_context_catalog_to_snapshot(
            build_workspace_context_catalog(
                load_workspace_settings_in_connection(
                    connection=connection,
                    user_id=user_id,
                )
            )
        )
    )


def _reconcile_folder_roots(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    manifest_id: str,
    created_at: str,
) -> None:
    """Mirror the user's active registered folders into the manifest's folder roots.

    ``source_type = 'folder'`` rows are owned here. A folder allowed for this
    conversation (``'approved_folder'``) gives way once the same folder is
    registered; every other root kind is left untouched.
    """
    stale_root_ids = connection.execute(
        """
        SELECT roots.root_id
        FROM workspace_manifest_roots AS roots
        WHERE roots.manifest_id = ?
          AND (
              (
                  roots.source_type = 'folder'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM workspace_folders AS folders
                      WHERE folders.user_id = ?
                        AND folders.folder_id = roots.source_id
                        AND folders.status = 'active'
                  )
              )
              OR (
                  roots.source_type = 'approved_folder'
                  AND EXISTS (
                      SELECT 1
                      FROM workspace_folders AS folders
                      WHERE folders.user_id = ?
                        AND folders.canonical_real_path = roots.canonical_real_path
                        AND folders.status = 'active'
                  )
              )
          )
        """,
        (manifest_id, user_id, user_id),
    ).fetchall()
    # A removed folder must stop being a root: a root row with every capability
    # off would still shadow an enclosing registered folder during path
    # resolution. file_references.root_id cascades on delete, so detach the
    # recorded references first to keep them.
    stale_params = [(str(row["root_id"]),) for row in stale_root_ids]
    connection.executemany(
        "UPDATE file_references SET root_id = NULL WHERE root_id = ?",
        stale_params,
    )
    connection.executemany(
        "DELETE FROM workspace_manifest_roots WHERE root_id = ?",
        stale_params,
    )
    rows = connection.execute(
        """
        SELECT
            folder_id,
            display_name,
            canonical_real_path,
            real_path
        FROM workspace_folders
        WHERE user_id = ? AND status = 'active'
        ORDER BY display_name ASC, folder_id ASC
        """,
        (user_id,),
    ).fetchall()
    root_rows = [
        (
            f"root:{manifest_id}:{str(row['folder_id'])}",
            manifest_id,
            str(row["folder_id"]),
            str(row["display_name"]),
            str(row["canonical_real_path"]),
            str(row["real_path"]),
            created_at,
        )
        for row in rows
    ]
    connection.executemany(
        """
        INSERT INTO workspace_manifest_roots(
            root_id,
            manifest_id,
            source_type,
            source_id,
            display_name,
            canonical_real_path,
            real_path,
            can_read,
            can_apply_patch,
            can_process_read,
            can_process_write,
            created_at
        ) VALUES (?, ?, 'folder', ?, ?, ?, ?, 1, 1, 1, 1, ?)
        ON CONFLICT(root_id) DO UPDATE SET
            display_name = excluded.display_name,
            real_path = excluded.real_path
        """,
        root_rows,
    )


def insert_approved_folder_root_in_connection(
    connection: sqlite3.Connection,
    *,
    manifest_id: str,
    approval_session_id: str,
    ordinal: int,
    folder: Path,
    created_at: str,
) -> None:
    folder_path = str(folder)
    # A second grant of the same folder, e.g. from another blocker of the same
    # turn, finds it already writable.
    connection.execute(
        """
        INSERT INTO workspace_manifest_roots(
            root_id,
            manifest_id,
            source_type,
            source_id,
            display_name,
            canonical_real_path,
            real_path,
            can_read,
            can_apply_patch,
            can_process_read,
            can_process_write,
            created_at
        ) VALUES (?, ?, 'approved_folder', ?, ?, ?, ?, 1, 1, 1, 1, ?)
        ON CONFLICT(manifest_id, canonical_real_path) DO NOTHING
        """,
        (
            # One approval can open several folders; each needs its own root.
            f"root:{manifest_id}:approved:{approval_session_id}:{ordinal}",
            manifest_id,
            approval_session_id,
            folder.name or folder_path,
            folder_path,
            folder_path,
            created_at,
        ),
    )


def _resolve_agent_experience_root(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    artifact_root: Path,
) -> AgentExperienceRoot | None:
    row = connection.execute(
        """
        SELECT revisions.revision_id, revisions.body_kind,
               revisions.artifact_root_path
        FROM memory_nodes AS nodes
        JOIN memory_revisions AS revisions
          ON revisions.user_id = nodes.user_id
         AND revisions.node_id = nodes.node_id
         AND revisions.revision_id = nodes.current_revision_id
        WHERE nodes.user_id = ?
          AND nodes.source_type = 'agent_experience'
          AND nodes.source_record_id = ?
          AND nodes.lifecycle = 'active'
          AND nodes.integrity = 'healthy'
        """,
        (user_id, user_id),
    ).fetchone()
    if row is None:
        return None
    if row["body_kind"] != "artifact_tree" or row["artifact_root_path"] is None:
        raise MigrationError(
            "healthy agent experience revision is not an artifact tree"
        )
    revision_root = resolve_artifact_path(
        root_path=artifact_root,
        relative_path=str(row["artifact_root_path"]),
    )
    experience_root = (revision_root / AGENT_EXPERIENCE_DIRNAME).resolve()
    try:
        experience_root.relative_to(revision_root)
    except ValueError as exc:
        raise MigrationError(
            "agent experience root escapes its current revision"
        ) from exc
    if not experience_root.is_dir():
        raise MigrationError(
            "healthy agent experience revision has no artifact directory"
        )
    index_path = (experience_root / AGENT_EXPERIENCE_INDEX_FILENAME).resolve()
    try:
        index_path.relative_to(experience_root)
    except ValueError as exc:
        raise MigrationError("agent experience index escapes its artifact") from exc
    if not index_path.is_file():
        raise MigrationError("healthy agent experience revision has no index.md")
    return AgentExperienceRoot(
        revision_id=str(row["revision_id"]),
        path=experience_root,
    )


def _insert_agent_experience_root(
    *,
    connection: sqlite3.Connection,
    manifest_id: str,
    root: AgentExperienceRoot,
    created_at: str,
) -> None:
    root_path = str(root.path)
    connection.execute(
        """
        INSERT INTO workspace_manifest_roots(
            root_id,
            manifest_id,
            source_type,
            source_id,
            display_name,
            canonical_real_path,
            real_path,
            can_read,
            can_apply_patch,
            can_process_read,
            can_process_write,
            created_at
        ) VALUES (?, ?, 'agent_experience', ?, 'Agent Experience', ?, ?, 1, 0, 0, 0, ?)
        """,
        (
            f"root:{manifest_id}:agent-experience",
            manifest_id,
            root.revision_id,
            root_path,
            root_path,
            created_at,
        ),
    )


__all__ = [
    "ensure_action_workspace_manifest",
    "insert_approved_folder_root_in_connection",
    "load_action_manifest_id",
]

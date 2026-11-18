from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ..action_session_temp_paths import (
    resolve_action_session_temp_leaf,
    resolve_action_storage_paths,
)
from ..models import (
    EXECUTION_SESSION_TERMINAL_STATUSES,
    ExecutionSessionTerminalStatus,
)
from .resource_db_support import configure_connection

type CleanupIntentStatus = Literal["active", "cleanup_failed"]


@dataclass(frozen=True, slots=True)
class ActionSessionTempCleanupReceipt:
    user_id: str
    action_id: str
    execution_session_id: str
    session_status: ExecutionSessionTerminalStatus
    canonical_leaf: Path
    manifest_id: str
    root_resource_id: str
    root_resource_status: CleanupIntentStatus
    root_resource_updated_at: str
    root_cleanup_attempts: int


@dataclass(frozen=True, slots=True)
class ActionSessionTempCleanupCandidate:
    user_id: str
    action_id: str
    execution_session_id: str


def list_terminal_action_session_temp_cleanup_candidates(
    *,
    db_path: Path,
    busy_timeout_ms: int,
) -> tuple[ActionSessionTempCleanupCandidate, ...]:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            """
            WITH RECURSIVE session_ancestors(
                user_id, action_id, execution_session_id,
                parent_execution_session_id, depth
            ) AS (
                SELECT DISTINCT session.user_id, session.action_id,
                       session.execution_session_id,
                       session.parent_execution_session_id, 0
                FROM tool_runtime_resources AS resource
                JOIN execution_sessions AS session
                  ON session.execution_session_id = resource.execution_session_id
                JOIN agent_actions AS action
                  ON action.action_id = session.action_id
                 AND action.user_id = session.user_id
                WHERE resource.resource_kind = 'temp_dir'
                  AND resource.action_id = session.action_id
                  AND resource.tool_invocation_id IS NULL
                  AND resource.status IN ('active', 'cleanup_failed')
                  AND session.status IN (
                      'completed', 'failed', 'canceled', 'expired'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jobs AS job
                      JOIN processes AS process
                        ON process.process_id = job.process_id
                      JOIN workspace_manifests AS manifest
                        ON manifest.user_id = session.user_id
                       AND manifest.action_id = session.action_id
                       AND manifest.execution_session_id = session.execution_session_id
                       AND manifest.status = 'ready'
                      WHERE job.user_id = session.user_id
                        AND job.job_type = 'execute_action'
                        AND job.status = 'running'
                        AND job.logical_key = session.action_id
                        AND process.user_id = session.user_id
                        AND process.kind = 'action'
                        AND process.status = 'running'
                        AND process.action_id = session.action_id
                        AND process.current_job_id = job.job_id
                  )
                UNION ALL
                SELECT ancestor.user_id, ancestor.action_id,
                       ancestor.execution_session_id,
                       parent.parent_execution_session_id,
                       ancestor.depth + 1
                FROM session_ancestors AS ancestor
                JOIN execution_sessions AS parent
                  ON parent.execution_session_id = ancestor.parent_execution_session_id
                 AND parent.user_id = ancestor.user_id
                 AND parent.action_id = ancestor.action_id
            )
            SELECT user_id, action_id, execution_session_id
            FROM session_ancestors
            WHERE parent_execution_session_id IS NULL
            ORDER BY user_id, action_id, depth DESC, execution_session_id
            """
        ).fetchall()
    return tuple(
        ActionSessionTempCleanupCandidate(
            user_id=str(row["user_id"]),
            action_id=str(row["action_id"]),
            execution_session_id=str(row["execution_session_id"]),
        )
        for row in rows
    )


def load_action_session_temp_cleanup_receipt(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    execution_session_id: str,
) -> ActionSessionTempCleanupReceipt | None:
    """Return immutable cleanup authority from one read-only SQLite snapshot."""

    resolved_db_path = db_path.resolve()
    paths = resolve_action_storage_paths(
        db_path=resolved_db_path,
        user_id=user_id,
        action_id=action_id,
    )
    canonical_leaf = resolve_action_session_temp_leaf(
        paths=paths,
        execution_session_id=execution_session_id,
    )
    database_uri = f"{resolved_db_path.as_uri()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            _CLEANUP_EVIDENCE_SQL,
            {
                "action_id": action_id,
                "canonical_session_root": str(paths.session_temp_root),
                "execution_session_id": execution_session_id,
                "user_id": user_id,
            },
        ).fetchone()
    return _build_receipt(row=row, canonical_leaf=canonical_leaf)


def _build_receipt(
    *, row: sqlite3.Row | None, canonical_leaf: Path
) -> ActionSessionTempCleanupReceipt | None:
    if row is None or any(
        int(row[field_name]) != expected
        for field_name, expected in (
            ("root_count", 1),
            ("manifest_count", 1),
            ("cycle_count", 0),
            ("bad_session_count", 0),
            ("bad_invocation_count", 0),
            ("bad_resource_count", 0),
            ("pending_approval_count", 0),
        )
    ):
        return None
    session_status = str(row["session_status"])
    root_status = str(row["root_status"])
    if (
        any(
            not isinstance(row[field_name], str) or not row[field_name].strip()
            for field_name in (
                "execution_session_id",
                "session_user_id",
                "session_action_id",
                "session_action_temp_dir",
                "action_user_id",
                "manifest_id",
                "manifest_user_id",
                "root_resource_id",
                "root_execution_session_id",
                "root_action_id",
                "root_resource_path",
                "root_updated_at",
            )
        )
        or str(row["session_user_id"]) != str(row["action_user_id"])
        or session_status not in EXECUTION_SESSION_TERMINAL_STATUSES
        or str(row["session_action_temp_dir"]) != str(canonical_leaf)
        or str(row["manifest_user_id"]) != str(row["session_user_id"])
        or str(row["manifest_status"]) != "ready"
        or str(row["root_execution_session_id"]) != str(row["execution_session_id"])
        or str(row["root_action_id"]) != str(row["session_action_id"])
        or str(row["root_resource_path"]) != str(canonical_leaf)
        or root_status not in {"active", "cleanup_failed"}
        or any(
            row[field_name] is not None
            for field_name in (
                "root_pid",
                "root_pgid",
                "root_process_start_signature",
                "root_lock_id",
                "root_cleaned_at",
            )
        )
    ):
        return None
    return ActionSessionTempCleanupReceipt(
        user_id=str(row["session_user_id"]),
        action_id=str(row["session_action_id"]),
        execution_session_id=str(row["execution_session_id"]),
        session_status=cast(ExecutionSessionTerminalStatus, session_status),
        canonical_leaf=canonical_leaf,
        manifest_id=str(row["manifest_id"]),
        root_resource_id=str(row["root_resource_id"]),
        root_resource_status=cast(CleanupIntentStatus, root_status),
        root_resource_updated_at=str(row["root_updated_at"]),
        root_cleanup_attempts=int(row["root_cleanup_attempts"]),
    )


_CLEANUP_EVIDENCE_SQL = """
WITH RECURSIVE
session_tree(
    execution_session_id, user_id, action_id, parent_execution_session_id,
    status, action_temp_dir, visited_ids_json, cycle_detected
) AS (
    SELECT execution_session_id, user_id, action_id, parent_execution_session_id,
           status, action_temp_dir, json_array(execution_session_id), 0
    FROM execution_sessions
    WHERE execution_session_id = :execution_session_id
    UNION
    SELECT child.execution_session_id, child.user_id, child.action_id,
           child.parent_execution_session_id, child.status, child.action_temp_dir,
           json_insert(parent.visited_ids_json, '$[#]', child.execution_session_id),
           EXISTS (
               SELECT 1 FROM json_each(parent.visited_ids_json)
               WHERE value = child.execution_session_id
           )
    FROM execution_sessions AS child
    JOIN session_tree AS parent
      ON child.parent_execution_session_id = parent.execution_session_id
    WHERE parent.cycle_detected = 0
),
target AS (
    SELECT session_tree.*, action.user_id AS action_user_id
    FROM session_tree
    LEFT JOIN agent_actions AS action ON action.action_id = session_tree.action_id
    WHERE session_tree.execution_session_id = :execution_session_id
      AND json_array_length(session_tree.visited_ids_json) = 1
),
root_intents AS (
    SELECT resource.*
    FROM tool_runtime_resources AS resource
    JOIN session_tree AS session
      ON session.execution_session_id = resource.execution_session_id
    WHERE resource.resource_kind = 'temp_dir'
      AND resource.action_id IS NOT NULL
      AND resource.tool_invocation_id IS NULL
),
root_summary AS (
    SELECT COUNT(*) AS root_count,
           MIN(resource_id) AS root_resource_id, MIN(execution_session_id) AS root_execution_session_id,
           MIN(action_id) AS root_action_id, MIN(status) AS root_status,
           MIN(resource_path) AS root_resource_path,
           MIN(updated_at) AS root_updated_at,
           MIN(cleanup_attempts) AS root_cleanup_attempts,
           MIN(pid) AS root_pid, MIN(pgid) AS root_pgid,
           MIN(process_start_signature) AS root_process_start_signature,
           MIN(lock_id) AS root_lock_id, MIN(cleaned_at) AS root_cleaned_at
    FROM root_intents
    WHERE execution_session_id = :execution_session_id
),
manifest_summary AS (
    SELECT COUNT(*) AS manifest_count,
           MIN(manifest_id) AS manifest_id,
           MIN(user_id) AS manifest_user_id,
           MIN(status) AS manifest_status
    FROM workspace_manifests
    WHERE action_id = :action_id
      AND user_id = :user_id
      AND status = 'ready'
      AND EXISTS (
          SELECT 1 FROM execution_sessions AS current_session
          WHERE current_session.execution_session_id = workspace_manifests.execution_session_id
            AND current_session.user_id = :user_id
            AND current_session.action_id = :action_id
      )
),
session_summary AS (
    SELECT COUNT(*) AS session_count,
           COALESCE(SUM(cycle_detected), 0) AS cycle_count,
           COALESCE(SUM(CASE WHEN
               user_id <> :user_id OR action_id IS NULL OR action_id <> :action_id
               OR trim(execution_session_id) = ''
               OR execution_session_id IN ('.', '..')
               OR instr(execution_session_id, '/') > 0
               OR instr(execution_session_id, char(92)) > 0
               OR instr(execution_session_id, char(0)) > 0
               OR status NOT IN ('completed', 'failed', 'canceled', 'expired')
               OR action_temp_dir IS NULL OR action_temp_dir <> (
                   :canonical_session_root || '/' || execution_session_id
               )
               OR 1 <> (
                   SELECT COUNT(*) FROM root_intents AS root
                   WHERE root.execution_session_id = session_tree.execution_session_id
               )
               OR EXISTS (
                   SELECT 1 FROM root_intents AS root
                   WHERE root.execution_session_id = session_tree.execution_session_id
                     AND (
                         trim(root.resource_id) = ''
                         OR root.action_id IS NOT :action_id
                         OR root.resource_path IS NOT session_tree.action_temp_dir
                         OR root.pid IS NOT NULL OR root.pgid IS NOT NULL
                         OR root.process_start_signature IS NOT NULL
                         OR root.lock_id IS NOT NULL
                         OR (session_tree.execution_session_id = :execution_session_id
                             AND root.status NOT IN ('active', 'cleanup_failed'))
                         OR (session_tree.execution_session_id <> :execution_session_id
                             AND root.status <> 'cleaned')
                     )
               )
           THEN 1 ELSE 0 END), 0) AS bad_session_count
    FROM session_tree
),
invocation_summary AS (
    SELECT COUNT(*) AS invocation_count,
           COALESCE(SUM(CASE WHEN
               invocation.user_id <> :user_id
               OR invocation.action_id <> :action_id
               OR invocation.manifest_id IS NULL
               OR invocation.manifest_id <> manifest.manifest_id
               OR invocation.status NOT IN (
                   'completed', 'failed', 'canceled', 'timed_out'
               )
           THEN 1 ELSE 0 END), 0) AS bad_invocation_count
    FROM tool_invocations AS invocation
    JOIN session_tree AS session
      ON session.execution_session_id = invocation.execution_session_id
    CROSS JOIN manifest_summary AS manifest
),
resource_summary AS (
    SELECT COUNT(*) AS resource_count,
           COALESCE(SUM(CASE
               WHEN root.root_count = 1
                AND resource.resource_id = root.root_resource_id THEN 0
               WHEN resource.action_id IS NULL
                 OR resource.action_id <> :action_id
                 OR resource.status <> 'cleaned'
                 OR (
                    resource.tool_invocation_id IS NOT NULL
                    AND (
                        invocation.invocation_id IS NULL
                        OR invocation.execution_session_id
                           <> resource.execution_session_id
                        OR invocation.user_id <> :user_id
                        OR invocation.action_id <> :action_id
                    )
                 )
               THEN 1 ELSE 0 END), 0) AS bad_resource_count
    FROM tool_runtime_resources AS resource
    JOIN session_tree AS session
      ON session.execution_session_id = resource.execution_session_id
    LEFT JOIN tool_invocations AS invocation
      ON invocation.invocation_id = resource.tool_invocation_id
    CROSS JOIN root_summary AS root
),
approval_summary AS (
    SELECT COUNT(*) AS pending_approval_count
    FROM approval_sessions
    WHERE action_id = :action_id AND status = 'pending'
)
SELECT target.execution_session_id, target.user_id AS session_user_id,
       target.action_id AS session_action_id, target.status AS session_status,
       target.action_temp_dir AS session_action_temp_dir, target.action_user_id,
       root_summary.*, manifest_summary.*, session_summary.*,
       invocation_summary.*, resource_summary.*, approval_summary.*
FROM target
CROSS JOIN root_summary CROSS JOIN manifest_summary CROSS JOIN session_summary
CROSS JOIN invocation_summary CROSS JOIN resource_summary CROSS JOIN approval_summary
"""

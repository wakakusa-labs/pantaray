from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.memory_update_lock import (
    MemoryUpdateLockConflictError,
    MemoryUpdateLockLease,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from . import repair_execution, revision_inspection
from .connection import open_memory_catalog_connection
from .errors import MemoryCatalogError, MemoryCatalogIntegrityError
from .observability import emit_memory_catalog_event
from .repair_queue import (
    list_due_repair_jobs,
    schedule_repair_retry,
)
from .resolver import enqueue_memory_repair

logger = logging.getLogger(__name__)


def reconcile_memory_catalog(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    scan_limit: int,
) -> tuple[int, int]:
    enqueued = _scan_current_heads(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        scan_limit=scan_limit,
    )
    jobs = list_due_repair_jobs(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        limit=scan_limit,
        now=now_utc_iso(),
    )
    completed = 0
    for job in jobs:
        try:
            with MemoryUpdateLockLease(
                root_path=artifact_root,
                user_id=job.user_id,
                owner_id=f"memory-repair:{job.node_id}",
            ):
                if repair_execution.repair_job(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    artifact_root=artifact_root,
                    job=job,
                ):
                    completed += 1
        except MemoryUpdateLockConflictError:
            continue
        except (MemoryCatalogError, OSError, sqlite3.Error, ValueError) as exc:
            schedule_repair_retry(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                job=job,
                error_code=type(exc).__name__,
            )
            emit_memory_catalog_event(
                "memory_link_repair_failed",
                user_id=job.user_id,
                node_id=job.node_id,
                revision_id=job.detected_revision_id,
                fault_code=job.reason,
            )
            logger.exception(
                "memory catalog repair failed",
                extra={
                    "user_id": job.user_id,
                    "node_id": job.node_id,
                    "revision_id": job.detected_revision_id,
                    "fault_code": job.reason,
                },
            )
    return enqueued, completed


def _scan_current_heads(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    scan_limit: int,
) -> int:
    if scan_limit <= 0:
        raise ValueError("memory repair scan_limit must be positive")
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        rows = _next_scan_rows(connection=connection, scan_limit=scan_limit)
        faults: list[tuple[str, str, str, str]] = []
        for row in rows:
            user_id = str(row["user_id"])
            node_id = str(row["node_id"])
            revision_id = str(row["current_revision_id"])
            try:
                inspection = revision_inspection.inspect_revision(
                    connection=connection,
                    artifact_root=artifact_root,
                    user_id=user_id,
                    revision_id=revision_id,
                )
                if inspection.needs_link_repair:
                    faults.append(
                        (user_id, node_id, revision_id, "link_manifest_mismatch")
                    )
            except revision_inspection.REVISION_INTEGRITY_ERRORS:
                faults.append((user_id, node_id, revision_id, "revision_integrity"))
        with immediate_transaction(connection):
            for user_id, node_id, revision_id, reason in faults:
                enqueue_memory_repair(
                    connection=connection,
                    user_id=user_id,
                    node_id=node_id,
                    detected_revision_id=revision_id,
                    reason=reason,
                )
            if rows:
                last = rows[-1]
                cursor = connection.execute(
                    """
                    UPDATE memory_catalog_reconcile_state
                    SET last_user_id = ?, last_node_id = ?, updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (
                        str(last["user_id"]),
                        str(last["node_id"]),
                        now_utc_iso(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise MemoryCatalogIntegrityError(
                        "memory reconcile cursor update failed"
                    )
    return len(faults)


def _next_scan_rows(
    *, connection: sqlite3.Connection, scan_limit: int
) -> tuple[sqlite3.Row, ...]:
    cursor = connection.execute(
        """
        SELECT last_user_id, last_node_id
        FROM memory_catalog_reconcile_state WHERE singleton_id = 1
        """
    ).fetchone()
    if cursor is None:
        raise MemoryCatalogIntegrityError("memory reconcile cursor is absent")
    last_user_id = cursor["last_user_id"]
    last_node_id = cursor["last_node_id"]
    if last_user_id is None:
        return tuple(
            connection.execute(
                """
                SELECT user_id, node_id, current_revision_id
                FROM memory_nodes
                WHERE lifecycle = 'active' AND integrity = 'healthy'
                ORDER BY user_id, node_id LIMIT ?
                """,
                (scan_limit,),
            ).fetchall()
        )
    after = connection.execute(
        """
        SELECT user_id, node_id, current_revision_id
        FROM memory_nodes
        WHERE lifecycle = 'active' AND integrity = 'healthy'
          AND (user_id > ? OR (user_id = ? AND node_id > ?))
        ORDER BY user_id, node_id LIMIT ?
        """,
        (last_user_id, last_user_id, last_node_id, scan_limit),
    ).fetchall()
    remaining = scan_limit - len(after)
    if remaining == 0:
        return tuple(after)
    wrapped = connection.execute(
        """
        SELECT user_id, node_id, current_revision_id
        FROM memory_nodes
        WHERE lifecycle = 'active' AND integrity = 'healthy'
          AND (user_id < ? OR (user_id = ? AND node_id <= ?))
        ORDER BY user_id, node_id LIMIT ?
        """,
        (last_user_id, last_user_id, last_node_id, remaining),
    ).fetchall()
    return (*after, *wrapped)

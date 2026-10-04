from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.runtime.process_lock import (
    acquire_runtime_process_lock,
    release_runtime_process_lock,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.memory_update_lock import (
    memory_update_lock_path,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_managed_action_workspace_root,
)

from .artifact_paths import (
    legacy_tenant_artifact_relative_path,
    tenant_artifact_relative_path,
)
from .connection import open_memory_catalog_connection
from .embedding_generations import delete_user_embedding_generations_in_transaction
from .erasure_descriptor_access import (
    UserErasureFilesystemTarget,
    open_user_erasure_root_descriptor,
    quarantine_user_erasure_targets,
    remove_user_erasure_directory,
    verify_user_erasure_paths_absent,
)
from .errors import MemoryCatalogIntegrityError
from .observability import emit_memory_catalog_event


@dataclass(frozen=True, slots=True)
class UserErasureResult:
    user_id: str
    erased: bool
    deletion_id: str | None


@dataclass(frozen=True, slots=True)
class UserErasureJob:
    user_id: str
    deletion_id: str
    artifact_path: str
    state: str


class ProtectedOwnerErasureError(ValueError):
    """Raised when erasure targets the installation's logged-out owner."""


def erase_user_memory_offline(
    *, db_path: Path, busy_timeout_ms: int, artifact_root: Path, user_id: str
) -> UserErasureResult:
    resolved_db_path = db_path.resolve()
    runtime_lock = acquire_runtime_process_lock(db_path=resolved_db_path)
    try:
        deletion_id = _start_erasure(
            db_path=resolved_db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
        )
        if deletion_id is None:
            return UserErasureResult(user_id=user_id, erased=False, deletion_id=None)
        emit_memory_catalog_event(
            "memory_user_erasure_started",
            user_id=user_id,
            deletion_id=deletion_id,
        )
        resume_pending_user_erasures(
            db_path=resolved_db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=artifact_root,
        )
        return UserErasureResult(user_id=user_id, erased=True, deletion_id=deletion_id)
    finally:
        release_runtime_process_lock(runtime_lock=runtime_lock)


def resume_pending_user_erasures(
    *, db_path: Path, busy_timeout_ms: int, artifact_root: Path
) -> int:
    jobs = _list_jobs(db_path=db_path, busy_timeout_ms=busy_timeout_ms)
    for job in jobs:
        try:
            _resume_job(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                artifact_root=artifact_root,
                job=job,
            )
        except (
            MemoryCatalogIntegrityError,
            OSError,
            sqlite3.DatabaseError,
            ValueError,
        ) as exc:
            emit_memory_catalog_event(
                "memory_user_erasure_failed",
                user_id=job.user_id,
                deletion_id=job.deletion_id,
                fault_code=type(exc).__name__,
            )
            raise
    return len(jobs)


def _start_erasure(*, db_path: Path, busy_timeout_ms: int, user_id: str) -> str | None:
    if not user_id.strip() or "/" in user_id or "\\" in user_id or ".." in user_id:
        raise ValueError("user_id is invalid for memory erasure")
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            existing = connection.execute(
                """
                SELECT deletion_id FROM memory_artifact_deletions
                WHERE user_id = ? AND reason = 'user_erasure'
                """,
                (user_id,),
            ).fetchone()
            if existing is not None:
                return str(existing["deletion_id"])
            if (
                connection.execute(
                    "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                is None
            ):
                return None
            if (
                connection.execute(
                    "SELECT 1 FROM local_owner WHERE user_id = ?", (user_id,)
                ).fetchone()
                is not None
            ):
                # The logged-out owner row is referenced with ON DELETE RESTRICT;
                # a queued erasure could never finish and would fail every startup.
                raise ProtectedOwnerErasureError(
                    "the logged-out owner cannot be erased through user erasure"
                )
            deletion_id = f"erase_{uuid.uuid4().hex}"
            relative_path = f"memory_catalog/erasure/{user_id}-{deletion_id}"
            now = now_utc_iso()
            connection.execute(
                """
                INSERT INTO memory_artifact_deletions(
                    user_id, deletion_id, artifact_path, reason, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'user_erasure', 'planned', ?, ?)
                """,
                (user_id, deletion_id, relative_path, now, now),
            )
            return deletion_id


def _resume_job(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    artifact_root: Path,
    job: UserErasureJob,
) -> None:
    _validate_user_erasure_job_path(job)
    targets = _user_erasure_filesystem_targets(user_id=job.user_id)
    root_descriptor = open_user_erasure_root_descriptor(root_path=artifact_root)
    try:
        current = job
        if current.state == "planned":
            quarantine_user_erasure_targets(
                root_descriptor=root_descriptor,
                quarantine_relative_path=current.artifact_path,
                targets=targets,
            )
            _set_state(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                job=current,
                expected="planned",
                state="quarantined",
            )
            current = UserErasureJob(
                current.user_id,
                current.deletion_id,
                current.artifact_path,
                "quarantined",
            )
        if current.state in {"quarantined", "database_detached"}:
            _remove_user_scratch_root(db_path=db_path, user_id=current.user_id)
        if current.state == "quarantined":
            _detach_tenant_database(
                db_path=db_path, busy_timeout_ms=busy_timeout_ms, job=current
            )
            current = UserErasureJob(
                current.user_id,
                current.deletion_id,
                current.artifact_path,
                "database_detached",
            )
        if current.state == "database_detached":
            remove_user_erasure_directory(
                root_descriptor=root_descriptor,
                relative_path=current.artifact_path,
            )
            _complete_erasure(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                root_descriptor=root_descriptor,
                job=current,
                targets=targets,
            )
    finally:
        os.close(root_descriptor)


def _remove_user_scratch_root(*, db_path: Path, user_id: str) -> None:
    storage_base = db_path.resolve().parent
    scratch_user_root = resolve_managed_action_workspace_root(db_path=db_path) / user_id
    root_descriptor = open_user_erasure_root_descriptor(root_path=storage_base)
    try:
        remove_user_erasure_directory(
            root_descriptor=root_descriptor,
            relative_path=scratch_user_root.relative_to(storage_base).as_posix(),
        )
    finally:
        os.close(root_descriptor)


def _detach_tenant_database(
    *, db_path: Path, busy_timeout_ms: int, job: UserErasureJob
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            connection.execute("PRAGMA defer_foreign_keys = ON")
            _delete_catalog_graph(connection=connection, user_id=job.user_id)
            connection.execute("DELETE FROM users WHERE user_id = ?", (job.user_id,))
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                tables = sorted({str(row[0]) for row in violations})
                raise MemoryCatalogIntegrityError(
                    "user erasure left foreign-key violations in: " + ", ".join(tables)
                )
            cursor = connection.execute(
                """
                UPDATE memory_artifact_deletions
                SET state = 'database_detached', updated_at = ?
                WHERE user_id = ? AND deletion_id = ? AND state = 'quarantined'
                """,
                (now_utc_iso(), job.user_id, job.deletion_id),
            )
            if cursor.rowcount != 1:
                raise MemoryCatalogIntegrityError("user erasure state changed")


def _delete_catalog_graph(*, connection: sqlite3.Connection, user_id: str) -> None:
    delete_user_embedding_generations_in_transaction(
        connection,
        user_id=user_id,
    )
    for table in (
        "memory_links",
        "memory_evidence_edges",
        "memory_revision_parents",
        "memory_repair_queue",
        "memory_revision_intents",
        "memory_fragments",
        "memory_revisions",
        "memory_nodes",
    ):
        connection.execute(f'DELETE FROM "{table}" WHERE user_id = ?', (user_id,))


def _complete_erasure(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    root_descriptor: int,
    job: UserErasureJob,
    targets: tuple[UserErasureFilesystemTarget, ...],
) -> None:
    verify_user_erasure_paths_absent(
        root_descriptor=root_descriptor,
        quarantine_relative_path=job.artifact_path,
        targets=targets,
    )
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            _verify_no_user_rows(connection=connection, user_id=job.user_id)
            connection.execute(
                """
                DELETE FROM memory_artifact_deletions
                WHERE user_id = ? AND deletion_id = ? AND state = 'database_detached'
                """,
                (job.user_id, job.deletion_id),
            )
    emit_memory_catalog_event(
        "memory_user_erasure_completed",
        user_id=job.user_id,
        deletion_id=job.deletion_id,
    )


def _verify_no_user_rows(*, connection: sqlite3.Connection, user_id: str) -> None:
    tables = connection.execute(
        """
        SELECT DISTINCT tables.name
        FROM sqlite_master AS tables
        JOIN pragma_table_info(tables.name) AS columns
        WHERE tables.type = 'table' AND columns.name = 'user_id'
          AND tables.name != 'memory_artifact_deletions'
        """
    ).fetchall()
    for row in tables:
        table = str(row["name"])
        if not table.replace("_", "").isalnum():
            raise MemoryCatalogIntegrityError("unexpected user-scoped table name")
        count = connection.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE user_id = ?', (user_id,)
        ).fetchone()[0]
        if int(count) != 0:
            raise MemoryCatalogIntegrityError(f"user erasure left rows in {table}")


def _set_state(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    job: UserErasureJob,
    expected: str,
    state: str,
) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            cursor = connection.execute(
                """
                UPDATE memory_artifact_deletions SET state = ?, updated_at = ?
                WHERE user_id = ? AND deletion_id = ? AND state = ?
                """,
                (state, now_utc_iso(), job.user_id, job.deletion_id, expected),
            )
            if cursor.rowcount != 1:
                raise MemoryCatalogIntegrityError("user erasure state changed")


def _list_jobs(*, db_path: Path, busy_timeout_ms: int) -> tuple[UserErasureJob, ...]:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        rows = connection.execute(
            """
            SELECT user_id, deletion_id, artifact_path, state
            FROM memory_artifact_deletions
            WHERE reason = 'user_erasure'
            ORDER BY created_at, deletion_id
            """
        ).fetchall()
    return tuple(
        UserErasureJob(
            user_id=str(row["user_id"]),
            deletion_id=str(row["deletion_id"]),
            artifact_path=str(row["artifact_path"]),
            state=str(row["state"]),
        )
        for row in rows
    )


def _user_erasure_filesystem_targets(
    *, user_id: str
) -> tuple[UserErasureFilesystemTarget, ...]:
    lock_path = memory_update_lock_path(
        root_path=Path("/"),
        user_id=user_id,
    ).relative_to(Path("/"))
    return (
        UserErasureFilesystemTarget(
            quarantine_name="catalog",
            relative_path=tenant_artifact_relative_path(user_id),
            kind="directory",
        ),
        UserErasureFilesystemTarget(
            quarantine_name="legacy",
            relative_path=legacy_tenant_artifact_relative_path(user_id),
            kind="directory",
        ),
        UserErasureFilesystemTarget(
            quarantine_name="memory_update_lock",
            relative_path=lock_path.as_posix(),
            kind="regular_file",
        ),
    )


def _validate_user_erasure_job_path(job: UserErasureJob) -> None:
    for value in (job.user_id, job.deletion_id):
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\0" in value
        ):
            raise MemoryCatalogIntegrityError("user erasure identity is invalid")
    expected = f"memory_catalog/erasure/{job.user_id}-{job.deletion_id}"
    if job.artifact_path != expected:
        raise MemoryCatalogIntegrityError("user erasure artifact path is invalid")

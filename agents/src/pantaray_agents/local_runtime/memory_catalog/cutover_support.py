from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    MarkdownReferenceOccurrence,
    extract_markdown_references,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .connection import open_memory_catalog_connection
from .cutover_records import LegacyMemoryRecord
from .draft import validate_memory_documents
from .fragments import artifact_content_sha256
from .models import MemoryDocument

CUTOVER_NAME = "memory_catalog_v1"


@dataclass(frozen=True, slots=True)
class LegacyReferenceMapping:
    user_id: str
    owner_memory_key: str
    local_ref_id: str
    target_memory_key: str
    created_at: str


def load_legacy_reference_mappings(
    connection: sqlite3.Connection,
) -> tuple[LegacyReferenceMapping, ...]:
    rows = connection.execute(
        """
        SELECT user_id, owner_memory_key, local_ref_id, target_memory_key, created_at
        FROM memory_record_references
        ORDER BY user_id, owner_memory_key, anchor_order
        """
    ).fetchall()
    return tuple(
        LegacyReferenceMapping(
            user_id=str(row["user_id"]),
            owner_memory_key=str(row["owner_memory_key"]),
            local_ref_id=str(row["local_ref_id"]),
            target_memory_key=str(row["target_memory_key"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    )


def all_occurrences(
    documents: tuple[MemoryDocument, ...],
) -> tuple[tuple[str, MarkdownReferenceOccurrence], ...]:
    return tuple(
        (document.source_path, occurrence)
        for document in documents
        for occurrence in extract_markdown_references(document.content)
    )


def occurrences_by_id(
    documents: tuple[MemoryDocument, ...],
) -> dict[str, tuple[MarkdownReferenceOccurrence, ...]]:
    grouped: dict[str, list[MarkdownReferenceOccurrence]] = {}
    for _, occurrence in all_occurrences(documents):
        grouped.setdefault(occurrence.local_ref_id, []).append(occurrence)
    return {key: tuple(value) for key, value in grouped.items()}


def source_markup(
    documents: tuple[MemoryDocument, ...],
    source_path: str,
    occurrence: MarkdownReferenceOccurrence,
) -> str:
    document = next(item for item in documents if item.source_path == source_path)
    return document.content[occurrence.match_start : occurrence.match_end]


def stable_node_id(record: LegacyMemoryRecord) -> str:
    identity = f"{record.user_id}\0{record.source}\0{record.source_record_id}"
    return f"mem_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def stable_revision_id(
    prefix: str, record: LegacyMemoryRecord, documents: tuple[MemoryDocument, ...]
) -> str:
    identity = f"{prefix}\0{record.user_id}\0{record.source}\0{record.source_record_id}\0{artifact_content_sha256(documents)}"
    return f"rev_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def write_artifact_tree(path: Path, documents: tuple[MemoryDocument, ...]) -> None:
    validate_memory_documents(documents)
    for document in documents:
        target = path / document.source_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            handle.write(document.content)
            handle.flush()
            os.fsync(handle.fileno())
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_preflight_backup(*, db_path: Path, artifact_root: Path) -> tuple[Path, Path]:
    # A backup file name, not a stored timestamp: compact and free of colons.
    suffix = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    db_backup = db_path.with_name(f"{db_path.name}.pre-memory-catalog-{suffix}.bak")
    artifact_backup = artifact_root.with_name(
        f"{artifact_root.name}.pre-memory-catalog-{suffix}.bak"
    )
    try:
        with closing(sqlite3.connect(db_path)) as source:
            with closing(sqlite3.connect(db_backup)) as target:
                source.backup(target)
        copied_artifacts = False
        for directory_name in ("memory", "memory_catalog"):
            source_root = artifact_root / directory_name
            if not source_root.exists():
                continue
            shutil.copytree(
                source_root,
                artifact_backup / directory_name,
                copy_function=shutil.copy2,
            )
            copied_artifacts = True
        if not copied_artifacts:
            artifact_backup.mkdir(parents=True)
    except (OSError, sqlite3.DatabaseError) as exc:
        cleanup_failures = _remove_incomplete_preflight_backup(
            db_backup=db_backup,
            artifact_backup=artifact_backup,
        )
        if cleanup_failures:
            raise ExceptionGroup(
                "preflight backup creation and cleanup failed",
                (exc, *cleanup_failures),
            ) from exc
        raise
    return db_backup, artifact_backup


def _remove_incomplete_preflight_backup(
    *, db_backup: Path, artifact_backup: Path
) -> tuple[OSError, ...]:
    failures: list[OSError] = []
    try:
        db_backup.unlink(missing_ok=True)
    except OSError as exc:
        failures.append(exc)
    try:
        shutil.rmtree(artifact_backup)
    except FileNotFoundError:
        pass
    except OSError as exc:
        failures.append(exc)
    return tuple(failures)


def record_empty_cutover(
    *, db_path: Path, busy_timeout_ms: int, inventory: dict[str, object]
) -> None:
    with (
        open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection,
        connection,
    ):
        now = now_utc_iso()
        connection.execute(
            """
            INSERT INTO memory_catalog_cutovers(
                cutover_name, state, inventory_json, started_at, completed_at
            ) VALUES (?, 'completed', ?, ?, ?)
            """,
            (CUTOVER_NAME, json.dumps(inventory, sort_keys=True), now, now),
        )


def record_failed_cutover(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    inventory: dict[str, object],
    error_code: str,
) -> None:
    with (
        open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection,
        connection,
    ):
        now = now_utc_iso()
        connection.execute(
            """
            INSERT INTO memory_catalog_cutovers(
                cutover_name, state, inventory_json, started_at, completed_at, error_code
            ) VALUES (?, 'failed', ?, ?, ?, ?)
            """,
            (CUTOVER_NAME, json.dumps(inventory, sort_keys=True), now, now, error_code),
        )

"""Save Memory-agent reference edits after the fixed-file cutover.

Callers hold the memory directory lock through every transaction and file write.
Mappings commit before new tags so a failed index update cannot lose their target.
The next index refresh prunes a mapping whose tag was never written.
"""

from __future__ import annotations

import secrets
import sqlite3

from pantaray_agents.local_runtime.memory_references.reference_parser import (
    remove_reference_ids,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .current_files_index import refresh_current_memory_index
from .editable_files import (
    MEMORY_AGENT_EDITABLE_ROOTS,
    MemoryFileConflictError,
    MemoryFiles,
    editable_memory_relative_path,
    memory_files_revision,
    validate_editable_memory_path,
)
from .epoch import resolve_context_handle
from .errors import (
    MemoryCatalogIntegrityError,
    MemoryContextExpiredError,
    MemoryLinkValidationError,
)
from .memory_run_binding import MemoryRunBinding, validate_memory_run_runtime
from .models import MemoryContextEpoch, MemoryDocument
from .reference_edits import insert_memory_reference


def link_current_memory_file(
    *,
    connection: sqlite3.Connection,
    files: MemoryFiles,
    binding: MemoryRunBinding,
    epoch: MemoryContextEpoch,
    target_handle: str,
    source_path: str,
    exact_text: str,
    occurrence: int,
    note: str,
    expected_revision: str,
) -> str:
    _require_editable_path(source_path)
    if epoch.run_id != binding.job_id:
        raise MemoryContextExpiredError("context epoch belongs to another Memory run")
    local_ref_id = f"ref_{secrets.token_hex(12)}"
    with immediate_transaction(connection):
        _require_writer(connection, files, binding)
        documents = _require_snapshot(files, expected_revision)
        refresh_current_memory_index(connection=connection, files=files)
        target = resolve_context_handle(
            epoch=epoch, user_id=binding.user_id, context_handle=target_handle
        )
        target_row = connection.execute(
            """
            SELECT fragments.content_text FROM memory_fragments AS fragments
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id AND revisions.revision_id = fragments.revision_id
            JOIN memory_nodes AS nodes
              ON nodes.user_id = revisions.user_id AND nodes.node_id = revisions.node_id
            WHERE fragments.user_id = ? AND fragments.fragment_id = ?
              AND nodes.lifecycle = 'active' AND nodes.integrity = 'healthy'
            """,
            (binding.user_id, target.fragment_id),
        ).fetchone()
        if target_row is None or str(target_row["content_text"]) != target.item.content:
            raise MemoryLinkValidationError(
                "target fragment is absent, unhealthy, or changed; search again"
            )
        source = connection.execute(
            """
            SELECT fragments.revision_id, fragments.fragment_id
            FROM memory_fragments AS fragments
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id AND revisions.revision_id = fragments.revision_id
            JOIN memory_nodes AS nodes
              ON nodes.user_id = fragments.user_id AND nodes.current_revision_id = fragments.revision_id
            WHERE fragments.user_id = ? AND fragments.source_path = ?
              AND fragments.block_kind = 'document_root'
              AND revisions.artifact_root_path = ?
            """,
            (
                binding.user_id,
                source_path,
                editable_memory_relative_path(binding.user_id),
            ),
        ).fetchone()
        if source is None:
            raise MemoryLinkValidationError("source memory file is absent")
        updated, _ = insert_memory_reference(
            documents=documents,
            source_path=source_path,
            exact_text=exact_text,
            occurrence=occurrence,
            local_ref_id=local_ref_id,
            note=note,
        )
        before = next(
            item.content for item in documents if item.source_path == source_path
        )
        after = next(
            item.content for item in updated if item.source_path == source_path
        )
        connection.execute(
            """
            INSERT INTO memory_links(
                user_id, source_revision_id, local_ref_id, source_fragment_id,
                target_fragment_id, reference_note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.user_id,
                str(source["revision_id"]),
                local_ref_id,
                str(source["fragment_id"]),
                target.fragment_id,
                note,
                now_utc_iso(),
            ),
        )
    with immediate_transaction(connection):
        # Cancellation may win the DB lock between the two commits.
        _require_writer(connection, files, binding)
        _require_snapshot(files, expected_revision)
        files.write(path=source_path, text=after, expected_text=before)
        refresh_current_memory_index(connection=connection, files=files)
    return local_ref_id


def unlink_current_memory_file(
    *,
    connection: sqlite3.Connection,
    files: MemoryFiles,
    binding: MemoryRunBinding,
    local_ref_id: str,
    expected_revision: str,
) -> None:
    with immediate_transaction(connection):
        _require_writer(connection, files, binding)
        documents = _require_snapshot(files, expected_revision)
        refresh_current_memory_index(connection=connection, files=files)
        rows = connection.execute(
            """
            SELECT fragments.source_path FROM memory_links AS links
            JOIN memory_fragments AS fragments
              ON fragments.user_id = links.user_id AND fragments.fragment_id = links.source_fragment_id
            JOIN memory_revisions AS revisions
              ON revisions.user_id = fragments.user_id AND revisions.revision_id = fragments.revision_id
            JOIN memory_nodes AS nodes
              ON nodes.user_id = links.user_id AND nodes.current_revision_id = links.source_revision_id
            WHERE links.user_id = ? AND links.local_ref_id = ?
              AND revisions.artifact_root_path = ?
            """,
            (
                binding.user_id,
                local_ref_id,
                editable_memory_relative_path(binding.user_id),
            ),
        ).fetchall()
        if len(rows) != 1:
            raise MemoryLinkValidationError(
                "local_ref_id is absent or ambiguous; re-read its file and remove the complete tag with apply_patch"
            )
        path = str(rows[0]["source_path"])
        _require_editable_path(path)
        before = next(item.content for item in documents if item.source_path == path)
        after = remove_reference_ids(before, {local_ref_id})
        files.write(path=path, text=after, expected_text=before)
        refresh_current_memory_index(connection=connection, files=files)


def _require_writer(
    connection: sqlite3.Connection, files: MemoryFiles, binding: MemoryRunBinding
) -> None:
    if files.user_id != binding.user_id:
        raise MemoryCatalogIntegrityError("memory files belong to another user")
    validate_memory_run_runtime(connection=connection, binding=binding)


def _require_snapshot(
    files: MemoryFiles, expected_revision: str
) -> tuple[MemoryDocument, ...]:
    documents = files.documents()
    if memory_files_revision(documents) != expected_revision:
        raise MemoryFileConflictError("memory files changed; re-read before editing")
    return documents


def _require_editable_path(path: str) -> None:
    validate_editable_memory_path(path)
    if path.partition("/")[0] not in MEMORY_AGENT_EDITABLE_ROOTS:
        raise MemoryLinkValidationError("Memory agent can edit only facts and insights")

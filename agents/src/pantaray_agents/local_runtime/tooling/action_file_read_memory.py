from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final, Literal
from urllib.parse import unquote

from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_memory_document,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryRevision,
)
from pantaray_agents.schema.tool_result import UnprojectedToolOutput
from pantaray_agents.tools.files.read_output import (
    ReadDocumentOutput,
    ReadTextPageOutput,
    ReadToolOutput,
)

from .models import ToolInvocationReadMemoryInput, ToolInvocationStatus

ACTION_FILE_READ_SOURCE: Final[Literal["action_file_read"]] = "action_file_read"
ACTION_FILE_READ_TOOL_ID: Final = "read"
_INTEGRATION_RUN_TOOL_ID: Final = "integration_run_tool"
_REFERENCE_PREFIX = "[[ref:"
_ESCAPED_REFERENCE_PREFIX = r"[\[ref:"


def register_action_file_read_memory(
    *,
    connection: sqlite3.Connection,
    invocation_id: str,
    user_id: str,
    completed_at: str,
    read_memory: ToolInvocationReadMemoryInput | None,
    redaction_applied: bool,
) -> MemoryRevision | None:
    if redaction_applied or read_memory is None:
        return None
    if _is_memory_folder_read(
        connection=connection,
        invocation_id=invocation_id,
        user_id=user_id,
        path=read_memory.path,
    ) or _is_integration_result_read(
        connection=connection,
        invocation_id=invocation_id,
        user_id=user_id,
        path=read_memory.path,
    ):
        return None
    revision = register_inline_memory_document(
        connection=connection,
        user_id=user_id,
        source=ACTION_FILE_READ_SOURCE,
        source_record_id=invocation_id,
        document=MemoryDocument(
            source_path=read_memory.source_path,
            content=read_memory.content,
        ),
    )
    cursor = connection.execute(
        """
        UPDATE memory_nodes
        SET created_at = ?, updated_at = ?
        WHERE user_id = ? AND node_id = ? AND current_revision_id = ?
        """,
        (
            completed_at,
            completed_at,
            user_id,
            revision.node_id,
            revision.revision_id,
        ),
    )
    if cursor.rowcount != 1:
        raise MemoryCatalogIntegrityError(
            "action file read memory timestamp update failed"
        )
    return revision


def build_action_file_read_memory_input(
    *,
    tool_id: str,
    status: ToolInvocationStatus,
    output: UnprojectedToolOutput,
) -> ToolInvocationReadMemoryInput | None:
    """Derive transient memory input from validated raw output before projection."""

    if tool_id != ACTION_FILE_READ_TOOL_ID or status != "completed":
        return None
    if isinstance(output, bytes):
        return None
    parsed = ReadToolOutput.model_validate(output).root
    if not isinstance(parsed, ReadTextPageOutput) or not parsed.content:
        return None
    source_path = (
        f"{parsed.path.removeprefix('/')}{_extraction_window(parsed)}"
        f"#L{parsed.offset}-L{parsed.end_line}"
    )
    return ToolInvocationReadMemoryInput(
        path=parsed.path,
        source_path=source_path,
        content=_render_search_document(parsed),
    )


def _extraction_window(read: ReadTextPageOutput) -> str:
    """Which part of a document the cited line numbers were counted in.

    A document extracted from a later unit numbers its lines from that unit, so
    without the unit two windows of one document cite the same lines for
    different text.
    """

    if not isinstance(read, ReadDocumentOutput) or read.start_unit == 1:
        return ""
    return f"#{read.unit_kind}{read.start_unit}"


def _is_memory_folder_read(
    *,
    connection: sqlite3.Connection,
    invocation_id: str,
    user_id: str,
    path: str,
) -> bool:
    # A read of the memory's own files is already memory.
    rows = connection.execute(
        """
        SELECT roots.canonical_real_path
        FROM tool_invocations AS invocations
        JOIN workspace_manifest_roots AS roots
          ON roots.manifest_id = invocations.manifest_id
        JOIN workspace_manifests AS manifests
          ON manifests.manifest_id = roots.manifest_id
        WHERE invocations.invocation_id = ?
          AND invocations.user_id = ?
          AND manifests.user_id = ?
          AND roots.source_type IN ('fact', 'long_term_insight', 'agent_experience')
          AND roots.can_read = 1
        """,
        (invocation_id, user_id, user_id),
    ).fetchall()
    read_path = Path(path).resolve()
    return any(
        read_path.is_relative_to(Path(str(row["canonical_real_path"])).resolve())
        for row in rows
    )


def _is_integration_result_read(
    *,
    connection: sqlite3.Connection,
    invocation_id: str,
    user_id: str,
    path: str,
) -> bool:
    # A large connected-app result is stored under the Action's tool results and
    # read back with `read`. It is still the user's mail, messages and files,
    # which never become memory documents.
    row = connection.execute(
        """
        SELECT roots.canonical_real_path
        FROM tool_invocations AS invocations
        JOIN workspace_manifest_roots AS roots
          ON roots.root_id = 'root:' || invocations.action_id || ':tool-results'
        WHERE invocations.invocation_id = ? AND invocations.user_id = ?
        """,
        (invocation_id, user_id),
    ).fetchone()
    if row is None:
        return False
    read_path = Path(path).resolve()
    results_root = Path(str(row["canonical_real_path"])).resolve()
    if not read_path.is_relative_to(results_root):
        return False
    # Each result is stored in a folder named after the invocation that produced it.
    producer = unquote(read_path.relative_to(results_root).parts[0])
    return (
        connection.execute(
            """
            SELECT 1 FROM tool_invocations
            WHERE invocation_id = ? AND user_id = ? AND tool_id = ?
            """,
            (producer, user_id, _INTEGRATION_RUN_TOOL_ID),
        ).fetchone()
        is not None
    )


def _render_search_document(read: ReadTextPageOutput) -> str:
    total_lines = str(read.total_lines) if read.total_lines is not None else "unknown"
    metadata = (
        f"Path: {read.path}\n\n"
        f"Observed lines: {read.offset}-{read.end_line} of {total_lines}\n\n"
    )
    return _escape_semantic_reference_markup(f"{metadata}{read.content}")


def _escape_semantic_reference_markup(text: str) -> str:
    # File contents are evidence, not authored memory. Keep ref-like text literal so it
    # cannot create a semantic link or mutate the original tool output.
    return text.replace(_REFERENCE_PREFIX, _ESCAPED_REFERENCE_PREFIX)


__all__ = [
    "build_action_file_read_memory_input",
    "register_action_file_read_memory",
]

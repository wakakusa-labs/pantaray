"""Memory editor session over the current files, activated by the runtime cutover."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.current_file_references import (
    link_current_memory_file,
    unlink_current_memory_file,
)
from pantaray_agents.local_runtime.memory_catalog.current_files_index import (
    refresh_current_memory_index,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MEMORY_AGENT_EDITABLE_ROOTS,
    MemoryFileConflictError,
    MemoryFiles,
    locked_memory_files,
    memory_files_revision,
)
from pantaray_agents.local_runtime.memory_catalog.memory_run_binding import (
    MemoryRunBinding,
    validate_memory_run_runtime,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.memory_catalog.reference_edits import (
    validate_reference_text_replacement,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.fs_sandbox import EditablePathPolicy
from pantaray_agents.local_runtime.tooling.memory_retrieval import MemoryContextSession
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)

from .logical_draft_io import require_editable_document_path
from .memory_domain_tools import memory_file_domain_definitions
from .tool_support import (
    FILE_TOOL_ERRORS,
    args_object,
    error_code,
    required_string,
    success,
)


@dataclass(frozen=True, slots=True)
class MemoryFileSnapshot:
    documents: tuple[MemoryDocument, ...]
    draft_revision: str


@dataclass(slots=True)
class CurrentMemoryFileSession:
    db_path: Path
    artifact_root: Path
    busy_timeout_ms: int
    binding: MemoryRunBinding
    memory_context: MemoryContextSession

    def __post_init__(self) -> None:
        if (self.binding.user_id, self.binding.job_id) != (
            self.memory_context.user_id,
            self.memory_context.run_id,
        ):
            raise ValueError(
                "memory editor and context belong to different users or runs"
            )

    @property
    def editable_policy(self) -> EditablePathPolicy:
        return EditablePathPolicy(
            tuple(f"{root}/**" for root in MEMORY_AGENT_EDITABLE_ROOTS)
        )

    @property
    def draft(self) -> MemoryFileSnapshot:
        # This is a tool snapshot, not a persisted editing generation.
        with locked_memory_files(
            artifact_root=self.artifact_root, user_id=self.binding.user_id
        ) as files:
            documents = files.documents()
            return MemoryFileSnapshot(documents, memory_files_revision(documents))

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return memory_file_domain_definitions(
            editable_policy=self.editable_policy,
            link=self.link,
            unlink=self.unlink,
            move_file=self.move_file,
            delete_file=self.delete_file,
        )

    def replace_document(
        self, *, path: str, content: str, expected_text: str, expected_revision: str
    ) -> str:
        self._require_path(path)
        validate_reference_text_replacement(
            path=path, before=expected_text, after=content
        )
        with self._mutation(expected_revision) as files:
            files.write(path=path, text=content, expected_text=expected_text)
            return memory_files_revision(files.documents())

    def append_document(
        self, *, path: str, content: str, expected_revision: str
    ) -> str:
        self._require_path(path)
        validate_reference_text_replacement(path=path, before="", after=content)
        with self._mutation(expected_revision) as files:
            files.write(path=path, text=content, expected_text=None)
            return memory_files_revision(files.documents())

    async def link(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = args_object(call.tool_args)
        try:
            occurrence = args.get("occurrence")
            if not isinstance(occurrence, int) or isinstance(occurrence, bool):
                raise ValueError("occurrence must be an integer")
            with self._open() as (files, connection):
                local_ref_id = link_current_memory_file(
                    connection=connection,
                    files=files,
                    binding=self.binding,
                    epoch=self.memory_context.require_epoch(),
                    target_handle=required_string(args, "target_handle"),
                    source_path=required_string(args, "source_path"),
                    exact_text=required_string(args, "exact_text"),
                    occurrence=occurrence,
                    note=required_string(args, "note"),
                    expected_revision=required_string(args, "expected_draft_revision"),
                )
                revision = memory_files_revision(files.documents())
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=error_code(exc), message=str(exc)
            )
        return success(
            call.tool_name,
            {
                "status": "success",
                "local_ref_id": local_ref_id,
                "draft_revision": revision,
            },
        )

    async def unlink(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = args_object(call.tool_args)
        local_ref_id = required_string(args, "local_ref_id")
        try:
            with self._open() as (files, connection):
                unlink_current_memory_file(
                    connection=connection,
                    files=files,
                    binding=self.binding,
                    local_ref_id=local_ref_id,
                    expected_revision=required_string(args, "expected_draft_revision"),
                )
                revision = memory_files_revision(files.documents())
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=error_code(exc), message=str(exc)
            )
        return success(
            call.tool_name,
            {
                "status": "success",
                "local_ref_id": local_ref_id,
                "draft_revision": revision,
            },
        )

    async def move_file(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = args_object(call.tool_args)
        try:
            source = required_string(args, "source_path")
            destination = required_string(args, "destination_path")
            self._require_path(source)
            self._require_path(destination)
            with self._mutation(
                required_string(args, "expected_draft_revision")
            ) as files:
                files.move(
                    source=source,
                    destination=destination,
                    expected_text=files.read(source),
                )
                revision = memory_files_revision(files.documents())
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=error_code(exc), message=str(exc)
            )
        return success(
            call.tool_name,
            {"status": "success", "path": destination, "draft_revision": revision},
        )

    async def delete_file(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = args_object(call.tool_args)
        try:
            path = required_string(args, "path")
            self._require_path(path)
            with self._mutation(
                required_string(args, "expected_draft_revision")
            ) as files:
                files.delete(path=path, expected_text=files.read(path))
                revision = memory_files_revision(files.documents())
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=error_code(exc), message=str(exc)
            )
        return success(
            call.tool_name,
            {"status": "success", "path": path, "draft_revision": revision},
        )

    def _require_path(self, path: str) -> None:
        require_editable_document_path(path=path, editable_policy=self.editable_policy)

    @contextmanager
    def _open(self) -> Iterator[tuple[MemoryFiles, sqlite3.Connection]]:
        with locked_memory_files(
            artifact_root=self.artifact_root, user_id=self.binding.user_id
        ) as files:
            with open_memory_catalog_connection(
                db_path=self.db_path, busy_timeout_ms=self.busy_timeout_ms
            ) as connection:
                yield files, connection

    @contextmanager
    def _mutation(self, expected_revision: str) -> Iterator[MemoryFiles]:
        with self._open() as (files, connection), immediate_transaction(connection):
            validate_memory_run_runtime(connection=connection, binding=self.binding)
            if memory_files_revision(files.documents()) != expected_revision:
                raise MemoryFileConflictError(
                    "memory files changed; re-read before editing"
                )
            yield files
            # If this fails, saved files remain authoritative; readers refresh the index.
            refresh_current_memory_index(connection=connection, files=files)

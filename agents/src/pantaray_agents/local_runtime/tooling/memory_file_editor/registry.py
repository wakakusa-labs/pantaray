from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from pantaray_agents.agents.memory_file_editor.tools import (
    APPLY_PATCH_TOOL_NAME,
    READ_FILE_TOOL_NAME,
    SEARCH_FILES_TOOL_NAME,
    SHELL_TOOL_NAME,
    WRITE_FILE_TOOL_NAME,
    apply_patch_request_schema,
    apply_patch_response_schema,
    shell_request_schema,
)
from pantaray_agents.local_runtime.memory_catalog.editable_files import (
    MemoryFileConflictError,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryLinkValidationError,
    MemoryPublicationConflictError,
)
from pantaray_agents.local_runtime.memory_references.reference_parser import (
    extract_markdown_references,
    has_reference_markup,
)
from pantaray_agents.local_runtime.tooling.fs_sandbox import (
    EditablePathPolicy,
    MemoryPatchError,
    SandboxShellError,
    TextFileErrorCode,
    apply_memory_patch,
    retry_advice_for_patch_error,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)

from .bounded_workspace_io import (
    DEFAULT_READ_LINE_LIMIT,
    SearchTruncationReason,
    read_text_page,
    search_text,
)
from .logical_draft_io import (
    draft_document_content,
    read_draft_text_page,
    require_editable_document_path,
    run_draft_shell_command,
    search_draft_documents,
)
from .memory_domain_tools import MemoryDraftSession
from .read_gate import (
    MemoryReadSnapshot,
    apply_patch_value_error_details,
    build_needs_read_output,
    merge_read_snapshot,
    read_snapshot_is_valid,
)
from .tool_support import FILE_TOOL_ERRORS
from .tool_support import (
    args_object as _args_object,
)
from .tool_support import (
    error_code as _error_code,
)
from .tool_support import (
    optional_int as _optional_int,
)
from .tool_support import (
    parse_patch_chunks as _parse_patch_chunks,
)
from .tool_support import (
    path_changed_response_schema as _path_changed_response_schema,
)
from .tool_support import (
    read_file_request_schema as _read_file_request_schema,
)
from .tool_support import (
    read_file_response_schema as _read_file_response_schema,
)
from .tool_support import (
    required_string as _required_string,
)
from .tool_support import (
    response_schema as _response_schema,
)
from .tool_support import (
    search_files_request_schema as _search_files_request_schema,
)
from .tool_support import (
    search_files_response_schema as _search_files_response_schema,
)
from .tool_support import (
    success as _success,
)
from .tool_support import (
    tool as _tool,
)
from .tool_support import (
    write_request_schema as _write_request_schema,
)


@dataclass(frozen=True, slots=True)
class ReadableFileRoot:
    root_id: str
    display_name: str
    path: Path
    canonical_path: Path


@dataclass(slots=True)
class LocalMemoryFileEditorTools:
    editable_policy: EditablePathPolicy
    memory_session: MemoryDraftSession
    readable_roots: tuple[ReadableFileRoot, ...] = ()
    read_snapshots: dict[str, MemoryReadSnapshot] = field(default_factory=dict)

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        tools = [
            _tool(
                name=READ_FILE_TOOL_NAME,
                description=self._read_description(
                    "Read one bounded page of a UTF-8 text file. Continue with "
                    "offset=next_offset and column=next_column when truncated. "
                    "This is the canonical source for apply_patch context/remove "
                    "lines when editing an existing file."
                ),
                request_schema=_read_file_request_schema(self._read_root_ids),
                response_schema=_read_file_response_schema(),
                execute=self.read_file,
            ),
            _tool(
                name=SEARCH_FILES_TOOL_NAME,
                description=self._read_description(
                    "Search UTF-8 text file paths and contents for a substring. "
                    "A path-only match has line_number 0. Responses disclose "
                    "whether the bounded traversal was truncated."
                ),
                request_schema=_search_files_request_schema(self._read_root_ids),
                response_schema=_search_files_response_schema(),
                execute=self.search_files,
            ),
            _tool(
                name=APPLY_PATCH_TOOL_NAME,
                description=(
                    "Apply a structured patch to one existing editable memory-draft "
                    "text file. Registered workspace roots are read-only. If the target "
                    "file has not been read in the current run, the patch is not applied; "
                    "the tool returns current text for a fresh patch. Existing refs may stay "
                    "while their text or headings change or move within this file. "
                    "Deleting a complete ref tag also unlinks it; the diff summary "
                    "reports removed ref IDs. Use link_memory to add refs. "
                    f"Editable paths: {self._editable_scope}."
                ),
                request_schema=apply_patch_request_schema(),
                response_schema=apply_patch_response_schema(),
                execute=self.apply_patch,
            ),
            _tool(
                name=WRITE_FILE_TOOL_NAME,
                description=(
                    "Create one new editable memory-draft UTF-8 text file. Registered "
                    "workspace roots are read-only. Fails if the path already exists. "
                    f"Editable paths: {self._editable_scope}."
                ),
                request_schema=_write_request_schema(),
                response_schema=_path_changed_response_schema(),
                execute=self.write_file,
            ),
            _tool(
                name=SHELL_TOOL_NAME,
                description=(
                    "Run hierarchy inspection inside the editable memory draft only. "
                    "This is not a general command runner; allowed commands are find, "
                    "ls, pwd, and test."
                ),
                request_schema=shell_request_schema(),
                response_schema=_response_schema(
                    required=(
                        "status",
                        "cmd",
                        "exit_code",
                        "stdout",
                        "stderr",
                        "changed_paths",
                    ),
                    properties={
                        "cmd": {"type": "string"},
                        "exit_code": {"type": "integer"},
                        "stdout": {"type": "string"},
                        "stderr": {"type": "string"},
                        "changed_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                ),
                execute=self.shell,
            ),
        ]
        tools.extend(self.memory_session.definitions())
        return tuple(tools)

    @property
    def _read_root_ids(self) -> tuple[str, ...]:
        return ("memory_draft", *(item.root_id for item in self.readable_roots))

    def _read_description(self, prefix: str) -> str:
        if not self.readable_roots:
            return f"{prefix} Allowed roots: memory_draft=editable memory draft."
        roots = "; ".join(
            f"{item.root_id}={item.display_name} ({item.path})"
            for item in self.readable_roots
        )
        return (
            f"{prefix} Allowed roots: memory_draft=editable memory draft; {roots}. "
            "Only memory_draft is editable."
        )

    @property
    def _editable_scope(self) -> str:
        return ", ".join(self.editable_policy.allowed_globs)

    async def read_file(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _args_object(call.tool_args)
        path = _required_string(args, "path")
        offset = _optional_int(args, "offset") or 1
        column = _optional_int(args, "column") or 1
        limit = _optional_int(args, "limit") or DEFAULT_READ_LINE_LIMIT
        draft_revision: str | None = None
        try:
            root_id = self._selected_root_id(args)
            if root_id == "memory_draft":
                snapshot = self.memory_session.draft
                draft_revision = snapshot.draft_revision
                page = read_draft_text_page(
                    documents=snapshot.documents,
                    path=path,
                    offset=offset,
                    column=column,
                    limit=limit,
                )
            else:
                root = self._external_root(root_id)
                page = await asyncio.to_thread(
                    read_text_page,
                    sandbox_root=root.path,
                    canonical_sandbox_root=root.canonical_path,
                    path=path,
                    offset=offset,
                    column=column,
                    limit=limit,
                )
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=_error_code(exc), message=str(exc)
            )
        if root_id == "memory_draft" and not page.truncated:
            self.read_snapshots[page.relative_path] = merge_read_snapshot(
                existing=self.read_snapshots.get(page.relative_path),
                path=page.relative_path,
                text=page.text,
                visible_texts=(page.text,),
                full_file=True,
                step_number=_step_number,
            )
        output: dict[str, JSONValue] = {
            "status": "success",
            "path": path,
            "text": page.text,
            "offset": page.offset,
            "column": page.column,
            "end_line": page.end_line,
            "end_column": page.end_column,
            "total_lines": page.total_lines,
            "next_offset": page.next_offset,
            "next_column": page.next_column,
            "truncated": page.truncated,
            "truncation_reason": page.truncation_reason,
            "retry_hint": page.retry_hint,
        }
        if draft_revision is not None:
            output["draft_revision"] = draft_revision
        return _success(call.tool_name, output)

    async def search_files(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _args_object(call.tool_args)
        query = _required_string(args, "query")
        limit = _optional_int(args, "limit") or 20
        try:
            root_id = self._selected_root_id(args)
            if root_id == "memory_draft":
                result = search_draft_documents(
                    documents=self.memory_session.draft.documents,
                    query=query,
                    limit=limit,
                )
            else:
                root = self._external_root(root_id)
                result = await asyncio.to_thread(
                    search_text,
                    sandbox_root=root.path,
                    canonical_sandbox_root=root.canonical_path,
                    query=query,
                    limit=limit,
                    match_paths=True,
                )
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=_error_code(exc), message=str(exc)
            )
        return _success(
            call.tool_name,
            {
                "status": "success",
                "matches": [
                    {
                        "path": match.path,
                        "line_number": match.line_number,
                        "line": match.line,
                    }
                    for match in result.matches
                ],
                "truncated": result.truncation_reason is not None,
                "truncation_reason": (
                    str(result.truncation_reason)
                    if result.truncation_reason is not None
                    else None
                ),
                "retry_hint": _search_retry_hint(result.truncation_reason),
                "skipped_files": result.skipped_files,
            },
        )

    def _selected_root_id(self, args: JSONValue) -> str:
        values = _args_object(args)
        root_id = _required_string(values, "root")
        if root_id != "memory_draft" and not any(
            item.root_id == root_id for item in self.readable_roots
        ):
            raise ValueError(f"unknown readable root: {root_id}")
        return str(root_id)

    def _external_root(self, root_id: str) -> ReadableFileRoot:
        root = next(
            (item for item in self.readable_roots if item.root_id == root_id), None
        )
        if root is None:
            raise ValueError(f"unknown readable root: {root_id}")
        return root

    async def apply_patch(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _args_object(call.tool_args)
        path = _required_string(args, "path")
        try:
            chunks = _parse_patch_chunks(args)
            canonical_path = require_editable_document_path(
                path=path,
                editable_policy=self.editable_policy,
            )
            snapshot = self.memory_session.draft
            canonical_path, base_text = draft_document_content(
                documents=snapshot.documents,
                path=canonical_path,
            )
            if not read_snapshot_is_valid(
                snapshot=self.read_snapshots.get(canonical_path),
                text=base_text,
                chunks=chunks,
            ):
                output, visible_texts = build_needs_read_output(
                    path=path,
                    text=base_text,
                    chunks=chunks,
                    snapshot=self.read_snapshots.get(canonical_path),
                )
                self.read_snapshots[canonical_path] = merge_read_snapshot(
                    existing=self.read_snapshots.get(canonical_path),
                    path=canonical_path,
                    text=base_text,
                    visible_texts=visible_texts,
                    full_file=output.get("read_scope") == "full_file",
                    step_number=_step_number,
                )
                return _success(call.tool_name, output)
            updated_text = apply_memory_patch(base_text, chunks)
            revision = self.memory_session.replace_document(
                path=canonical_path,
                content=updated_text,
                expected_text=base_text,
                expected_revision=snapshot.draft_revision,
            )
            self._invalidate_path(canonical_path)
        except (MemoryPublicationConflictError, MemoryFileConflictError) as exc:
            self._invalidate_path(path)
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_DRAFT_REVISION_STALE",
                message=str(exc),
                details={
                    "path": path,
                    "retry_advice": "Re-read the target file before retrying.",
                },
            )
        except MemoryLinkValidationError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="MEMORY_LINK_INVALID",
                message=str(exc),
                details={
                    "path": path,
                    "retry_advice": "Use link_memory to add refs or recreate them in another file. "
                    "Preserve existing tags intact and unique, or remove complete tags to unlink them. "
                    "Agent Experience evidence is host-owned.",
                },
            )
        except MemoryPatchError as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=str(exc.code),
                message=str(exc),
                details={
                    "path": path,
                    "retry_advice": retry_advice_for_patch_error(exc.code, str(exc)),
                },
            )
        except FILE_TOOL_ERRORS as exc:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=_error_code(exc),
                message=str(exc),
                details={
                    "path": path,
                    "retry_advice": "Re-read the target file and retry with an editable path.",
                },
            )
        except ValueError as exc:
            error_code, retry_advice = apply_patch_value_error_details(exc)
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=error_code,
                message=str(exc),
                details={"path": path, "retry_advice": retry_advice},
            )
        remaining_refs = {
            item.local_ref_id for item in extract_markdown_references(updated_text)
        }
        removed_refs = [
            item.local_ref_id
            for item in extract_markdown_references(base_text)
            if item.local_ref_id not in remaining_refs
        ]
        summary = f"updated {path}"
        if removed_refs:
            summary += "; unlinked refs: " + ", ".join(removed_refs)
        return _success(
            call.tool_name,
            {
                "status": "success",
                "path": path,
                "changed": updated_text != base_text,
                "diff_summary": summary,
                "draft_revision": revision,
            },
        )

    async def write_file(
        self, call: ReactToolCall, _step_number: int
    ) -> ReactToolResult:
        args = _args_object(call.tool_args)
        path = _required_string(args, "path")
        text = _required_string(args, "text")
        if has_reference_markup(text):
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=str(TextFileErrorCode.UNSUPPORTED_FILE),
                message="new files cannot contain raw ref markup; use link_memory",
            )
        try:
            snapshot = self.memory_session.draft
            revision = self.memory_session.append_document(
                path=path, content=text, expected_revision=snapshot.draft_revision
            )
            self._invalidate_path(path)
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=_error_code(exc), message=str(exc)
            )
        return _success(
            call.tool_name,
            {
                "status": "success",
                "path": path,
                "changed": True,
                "draft_revision": revision,
            },
        )

    async def shell(self, call: ReactToolCall, _step_number: int) -> ReactToolResult:
        args = _args_object(call.tool_args)
        cmd = _required_string(args, "cmd")
        try:
            exit_code, stdout, stderr = run_draft_shell_command(
                documents=self.memory_session.draft.documents,
                cmd=cmd,
            )
        except (*FILE_TOOL_ERRORS, ValueError) as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=_error_code(exc), message=str(exc)
            )
        except SandboxShellError as exc:
            return tool_error_response(
                tool_name=call.tool_name, error_code=exc.code, message=str(exc)
            )
        return _success(
            call.tool_name,
            {
                "status": "success",
                "cmd": cmd.strip(),
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "changed_paths": [],
            },
        )

    def _invalidate_path(self, path: str) -> None:
        self.read_snapshots.pop(path, None)


def build_local_memory_file_tools(
    *,
    editable_policy: EditablePathPolicy,
    memory_session: MemoryDraftSession,
    readable_roots: tuple[ReadableFileRoot, ...] = (),
) -> tuple[ReactToolDefinition, ...]:
    return LocalMemoryFileEditorTools(
        editable_policy=editable_policy,
        memory_session=memory_session,
        readable_roots=readable_roots,
    ).definitions()


def _search_retry_hint(reason: SearchTruncationReason | None) -> str | None:
    if reason is None:
        return None
    if reason == SearchTruncationReason.MATCH_LIMIT:
        return "Use a narrower query or raise limit before searching again."
    return "Use a narrower query or a smaller registered root before searching again."

"""read/list/glob/grep as ``ReactToolDefinition``s for an agent without a broker.

The bodies are the Action's (``read``, ``discovery``). This module builds their
``ReadScope`` over the registered folders, turns a refusal into a coded tool
error, and writes a result too large to keep inline to a file that the same
tools can read back, as the Action's tool results do.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ValidationError

from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    release_stored_tool_result,
    store_tool_result,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    ReadAccessScope,
)
from pantaray_agents.tools.contract import (
    REQUEST_VALIDATION_MESSAGE,
    BrokerPolicyError,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolExecutor,
    ReactToolResult,
    ToolImage,
    react_tool_response_schema,
    tool_error_response,
)

from .discovery import GREP_MAX_OUTPUT_BYTES, run_glob, run_grep, run_list
from .grep_lines import GREP_MAX_LINE_CHARS
from .manifest_paths import PATH_NOT_ABSOLUTE, ManifestRoot
from .private_storage import PrivateAppStorage
from .read import run_read
from .read_contract import (
    DISCOVERY_RESULT_LIMIT_MAX,
    LIST_MAX_DEPTH,
    GlobToolArgs,
    GrepToolArgs,
    ListToolArgs,
    ReadToolArgs,
    ReadToolResult,
)
from .read_scope import ReadScope
from .ripgrep import RIPGREP_TIMEOUT_SECONDS

_SPILL_ROOT_MODE = 0o700
_MEMORY_NOTE = (
    "Pantaray's memory (facts, insights and their TODOs, agent experience, "
    "activity) is not a file here: read it with memory_search and "
    "get_memory_reference."
)
_SPILL_NOTE = (
    "A result too large to show inline is saved to a file instead; the result "
    "then gives its path, a preview and how to read the rest with read or grep."
)
_PATH: dict[str, JSONValue] = {"type": "string", "minLength": 1, "pattern": r"\S"}
_POSITIVE: dict[str, JSONValue] = {"type": "integer", "minimum": 1}
_SPILLED: dict[str, JSONValue] = {"required": ["storage", "path"]}
# run_read and the discovery bodies, each called as body(scope=..., request=...).
type _Body = Callable[..., ReadToolResult]


def build_read_only_file_tools(
    *,
    folders: tuple[Path, ...],
    read_access_scope: ReadAccessScope,
    app_storage_roots: tuple[Path, ...],
    spill_root: Path,
) -> tuple[ReactToolDefinition, ...]:
    """The four tools over ``folders``, which they only read.

    Paths are absolute: there is no current directory and no scratch workspace.
    ``app_storage_roots`` stay hidden except ``spill_root``, created here, where
    a large result is written and read back from.
    """

    spill_root.mkdir(mode=_SPILL_ROOT_MODE, parents=True, exist_ok=True)
    spill_path = spill_root.resolve(strict=True)
    scope = ReadScope(
        # The spill root comes first: a registered folder may enclose app storage.
        manifest_roots=(
            _read_only_root("tool_results", spill_path),
            *(
                _read_only_root(f"folder:{index}", folder)
                for index, folder in enumerate(folders)
            ),
        ),
        cwd_path=None,
        read_access_scope=read_access_scope,
        scratch_root_path=None,
        private_storage=PrivateAppStorage(
            storage_roots=app_storage_roots, readable_roots=(spill_path,)
        ),
    )
    return _ReadOnlyFileTools(scope=scope, spill_root=spill_path).definitions()


def _read_only_root(root_id: str, path: Path) -> ManifestRoot:
    return ManifestRoot(
        root_id=root_id,
        manifest_id="read_only",
        source_type="folder",
        display_name=str(path),
        canonical_real_path=path,
        real_path=path,
        can_read=True,
        can_apply_patch=False,
        can_process_read=False,
        can_process_write=False,
    )


@dataclass(frozen=True, slots=True)
class _ReadOnlyFileTools:
    scope: ReadScope
    spill_root: Path

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        where = (
            "Any local path is readable"
            if self.scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
            else "Readable paths are inside the registered folders"
        ) + "; Pantaray's own storage is hidden."
        notes = f"{where} {_MEMORY_NOTE} {_SPILL_NOTE}"
        return (
            _definition(
                name="read",
                description=(
                    "Read one file or directory by its absolute path. Text comes "
                    "a page at a time: while next_offset is not null, continue "
                    "with offset=next_offset and column=next_column; when "
                    "truncated is true, follow truncation_reason and retry_hint. "
                    "A .pdf, .docx, .xlsx, .pptx or .ipynb file comes back as "
                    "kind=document: extracted text on the same cursor, with an "
                    "outline and notes on what extraction left out; on "
                    "truncation_reason=document_budget continue with "
                    "start_unit=next_start_unit. An image file comes back as the "
                    "image to look at, where this run can show images. " + notes
                ),
                properties={
                    "path": _PATH,
                    "offset": _POSITIVE,
                    "column": _POSITIVE,
                    "limit": _POSITIVE,
                    "start_unit": _POSITIVE,
                },
                required=("path",),
                success={"anyOf": [{"required": ["kind", "path"]}, _SPILLED]},
                execute=self._executor(ReadToolArgs, run_read),
            ),
            _definition(
                name="list",
                description=(
                    "List files and directories under an absolute directory "
                    "path, max_depth levels deep (1 lists its own entries), in "
                    "path order, stopping at limit entries. Symlinks are not "
                    "followed; warning and retry_hint say what was left out. " + notes
                ),
                properties={
                    "path": _PATH,
                    "max_depth": {**_POSITIVE, "maximum": LIST_MAX_DEPTH},
                    "limit": {**_POSITIVE, "maximum": DISCOVERY_RESULT_LIMIT_MAX},
                },
                required=("path",),
                success=_DISCOVERY_SUCCESS,
                execute=self._executor(ListToolArgs, run_list),
            ),
            _definition(
                name="glob",
                description=(
                    "Find files by a glob pattern, such as **/*.md, relative to "
                    "an absolute base_path, stopping at limit matches. Symlinks "
                    f"are not followed; the search stops after "
                    f"{RIPGREP_TIMEOUT_SECONDS:g} seconds; warning and "
                    "retry_hint say what was left out. " + notes
                ),
                properties={
                    "base_path": _PATH,
                    "pattern": _PATH,
                    "limit": {**_POSITIVE, "maximum": DISCOVERY_RESULT_LIMIT_MAX},
                },
                required=("base_path", "pattern"),
                success=_DISCOVERY_SUCCESS,
                execute=self._executor(GlobToolArgs, run_glob),
            ),
            _definition(
                name="grep",
                description=(
                    "Search text files of any size under an absolute base_path "
                    "for a ripgrep regular expression; include_glob, relative "
                    "to base_path, narrows the files. A line over "
                    f"{GREP_MAX_LINE_CHARS} characters comes back as an excerpt "
                    "around its first match and a matching binary file is named "
                    "in warning. Output stops at max_matches lines or "
                    f"{GREP_MAX_OUTPUT_BYTES // 1024} KB, and the search after "
                    f"{RIPGREP_TIMEOUT_SECONDS:g} seconds; warning and retry_hint "
                    "say what was left out. " + notes
                ),
                properties={
                    "base_path": _PATH,
                    "pattern": _PATH,
                    "include_glob": _PATH,
                    "max_matches": {
                        **_POSITIVE,
                        "maximum": DISCOVERY_RESULT_LIMIT_MAX,
                    },
                },
                required=("base_path", "pattern"),
                success=_DISCOVERY_SUCCESS,
                execute=self._executor(GrepToolArgs, run_grep),
            ),
        )

    def _executor(self, args_model: type[BaseModel], body: _Body) -> ReactToolExecutor:
        async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
            try:
                request = args_model.model_validate(call.tool_args)
            except ValidationError as exc:
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code="TOOL_REQUEST_VALIDATION_ERROR",
                    message=REQUEST_VALIDATION_MESSAGE,
                    details={"message": str(exc)},
                )
            try:
                # A read can stream far into a large file and a spill syncs a
                # file to disk, so both stay off the event loop.
                return await asyncio.to_thread(self._run, call, body, request)
            except BrokerPolicyError as exc:
                details: dict[str, JSONValue] = {}
                if exc.code == PATH_NOT_ABSOLUTE:
                    # A relative path here is often a memory path such as
                    # insights/todos.md, which these tools cannot open.
                    details["fix_hint"] = f"{exc.fix_hint} {_MEMORY_NOTE}"
                elif exc.fix_hint:
                    details["fix_hint"] = exc.fix_hint
                if exc.examples:
                    details["examples"] = list(exc.examples)
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code=exc.code,
                    message=str(exc),
                    details=details or None,
                )
            # A file that cannot be read fails only its own call, as it does in
            # the Action, and the run goes on with other sources.
            except OSError as exc:
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code=f"{call.tool_name.upper()}_FAILED",
                    message=str(exc) or type(exc).__name__,
                )

        return execute

    def _run(
        self, call: ReactToolCall, body: _Body, request: BaseModel
    ) -> ReactToolResult:
        result = body(scope=self.scope, request=request)
        stored = store_tool_result(
            action_tool_results_path=self.spill_root,
            invocation_id=uuid.uuid4().hex,
            output=result.output,
        )
        release_error = release_stored_tool_result(stored)
        if release_error is not None:
            raise release_error
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output=stored.output_json,
            images=tuple(_image(attachment) for attachment in result.attachments),
        )


_DISCOVERY_SUCCESS: dict[str, JSONValue] = {
    "anyOf": [
        {"required": ["status"], "properties": {"status": {"const": "success"}}},
        _SPILLED,
    ]
}


def _definition(
    *,
    name: str,
    description: str,
    properties: dict[str, JSONValue],
    required: tuple[str, ...],
    success: dict[str, JSONValue],
    execute: ReactToolExecutor,
) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description=description,
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
        },
        response_schema=react_tool_response_schema(
            success_schema={"type": "object", **success}
        ),
        execute=execute,
    )


def _image(attachment: Mapping[str, JSONValue]) -> ToolImage:
    """An image ``read`` attached, as the workspace file it read."""

    return ToolImage(
        ref=str(attachment["ref"]),
        blob_ref=str(attachment["blob_ref"]),
        display_path=str(attachment["display_path"]),
        mime_type=str(attachment["mime_type"]),
        byte_size=cast(int, attachment["byte_size"]),
        sha256=str(attachment["sha256"]),
        workspace_root_path=str(attachment["workspace_root_path"]),
        workspace_relative_path=str(attachment["workspace_relative_path"]),
    )


__all__ = ["build_read_only_file_tools"]

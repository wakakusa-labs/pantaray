"""read/list/glob/grep as ``ReactToolDefinition``s for an agent without a broker.

The bodies are the Action's (``read``, ``discovery``). This module builds their
``ReadScope`` over the registered folders, turns a refusal into a coded tool
error, and writes a result too large to keep inline to a file that the same
tools can read back, as the Action's tool results do.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ValidationError

from pantaray_agents.local_runtime.tooling.documents import (
    MAX_RENDERED_PAGES,
    RENDERED_PAGE_MEDIA_TYPE,
)
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    store_run_tool_result,
)
from pantaray_agents.schema.action_conversation import RENDERER_PREPARING_OUTPUT_KIND
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
    ReadAccessScope,
)
from pantaray_agents.tools.contract import (
    REQUEST_VALIDATION_MESSAGE,
    BrokerPolicyError,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolExecutor,
    ReactToolResult,
    ToolConcurrency,
    ToolImage,
    react_tool_response_schema,
    tool_error_response,
)

from .attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    ATTACHMENT_ID_HEX_LENGTH,
    TOOL_ATTACHMENT_REF_PREFIX,
    ReadImage,
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
from .render_pages import (
    CONTINUE_WITH_READ,
    DrawnDocument,
    RendererPreparing,
    RenderPdfPageToolArgs,
    draw_document_pages,
)
from .ripgrep import DISCOVERY_IGNORE_NOTE, RIPGREP_TIMEOUT_SECONDS

_SPILL_ROOT_MODE = 0o700
_MEMORY_NOTE = (
    "Pantaray's memory (facts, insights and their TODOs, agent experience, "
    "activity) is not a file here: read it with memory_search and "
    "get_memory_reference."
)
_MEMORY_FOLDERS_NOTE = (
    "Pantaray's memory folders are readable too: {folders}. A facts/ or "
    "insights/ path in the memory indexes is under the folder of that name. "
    "Read activity with memory_search and get_memory_reference."
)
_SPILL_NOTE = (
    "When a result is too large to show inline, its longest texts are saved to "
    "files instead; each then gives its path, a preview and how to read the rest "
    "with read or grep."
)
_PATH: dict[str, JSONValue] = {"type": "string", "minLength": 1, "pattern": r"\S"}
_POSITIVE: dict[str, JSONValue] = {"type": "integer", "minimum": 1}
_SPILLED: dict[str, JSONValue] = {"required": ["storage", "path"]}
# run_read and the discovery bodies, each called as body(scope=..., request=...).
type _Body = Callable[..., ReadToolResult]
type _RunCall = Callable[[ReactToolCall, BaseModel], Awaitable[ReactToolResult]]
RENDER_PDF_PAGE_TOOL_NAME = "render_pdf_page"
# Inside the run's own folder, so both go with it when the run ends.
_PAGES_DIRNAME = "rendered_pages"
_READ_IMAGES_DIRNAME = "read_images"
_CONVERTED_DIRNAME = "converted_documents"


def build_read_only_file_tools(
    *,
    db_path: Path,
    folders: tuple[Path, ...],
    read_access_scope: ReadAccessScope,
    app_storage_roots: tuple[Path, ...],
    spill_root: Path,
    memory_folders: tuple[Path, ...] = (),
    own_folder: Path | None = None,
) -> tuple[ReactToolDefinition, ...]:
    """The tools over ``folders`` and ``memory_folders``, which they only read.

    Paths are absolute: there is no current directory and no scratch workspace.
    ``app_storage_roots`` stay hidden except ``memory_folders``, ``own_folder``
    (what the run itself made, such as a Suggestion's artifact folder) and
    ``spill_root``, created here: the run's own folder, where a large result is
    written and read back from and drawn pages and converted documents are
    kept, all removed with it.
    """

    spill_root.mkdir(mode=_SPILL_ROOT_MODE, parents=True, exist_ok=True)
    spill_path = spill_root.resolve(strict=True)
    own = () if own_folder is None else (own_folder,)
    scope = ReadScope(
        # The spill root comes first: a registered folder may enclose app storage.
        manifest_roots=(
            _read_only_root("tool_results", spill_path),
            *(_read_only_root("own", folder) for folder in own),
            *(
                _read_only_root(f"memory:{index}", folder)
                for index, folder in enumerate(memory_folders)
            ),
            *(
                _read_only_root(f"folder:{index}", folder)
                for index, folder in enumerate(folders)
            ),
        ),
        cwd_path=None,
        read_access_scope=read_access_scope,
        scratch_root_path=None,
        private_storage=PrivateAppStorage(
            storage_roots=app_storage_roots,
            readable_roots=(spill_path, *own, *memory_folders),
        ),
    )
    memory_note = (
        _MEMORY_FOLDERS_NOTE.format(
            folders=", ".join(f"`{folder}`" for folder in memory_folders)
        )
        if memory_folders
        else _MEMORY_NOTE
    )
    return _ReadOnlyFileTools(
        db_path=db_path, scope=scope, spill_root=spill_path, memory_note=memory_note
    ).definitions()


def build_saved_result_read_tool(
    *, db_path: Path, spill_root: Path
) -> ReactToolDefinition:
    """read alone, over nothing but ``spill_root``.

    For a run whose own file tools cannot reach the folder where its large
    results are saved, so it can follow a saved result's hint to read the rest.
    """

    spill_path = spill_root.resolve(strict=True)
    tools = _ReadOnlyFileTools(
        db_path=db_path,
        scope=ReadScope(
            manifest_roots=(_read_only_root("tool_results", spill_path),),
            cwd_path=None,
            read_access_scope=READ_ACCESS_SCOPE_WORKSPACE,
            scratch_root_path=None,
            private_storage=PrivateAppStorage(
                storage_roots=(), readable_roots=(spill_path,)
            ),
        ),
        spill_root=spill_path,
        memory_note="Its only folder is this run's saved tool results.",
    )
    return next(tool for tool in tools.definitions() if tool.name == "read")


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
    db_path: Path  # where the Office converter is installed from
    scope: ReadScope
    spill_root: Path
    memory_note: str

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        where = (
            "Any local path is readable"
            if self.scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS
            else "Readable paths are inside the registered folders"
        ) + "; Pantaray's own storage is hidden."
        notes = f"{where} {self.memory_note} {_SPILL_NOTE}"
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
                execute=self._executor(ReadToolArgs, self._off_loop(run_read)),
            ),
            _definition(
                name="list",
                description=(
                    "List files and directories under an absolute directory "
                    "path, max_depth levels deep (1 lists its own entries), in "
                    "path order, stopping at limit entries. Symlinks are not "
                    "followed; warning and retry_hint say what was left out. "
                    f"{DISCOVERY_IGNORE_NOTE} {notes}"
                ),
                properties={
                    "path": _PATH,
                    "max_depth": {**_POSITIVE, "maximum": LIST_MAX_DEPTH},
                    "limit": {**_POSITIVE, "maximum": DISCOVERY_RESULT_LIMIT_MAX},
                },
                required=("path",),
                success=_DISCOVERY_SUCCESS,
                execute=self._executor(ListToolArgs, self._off_loop(run_list)),
            ),
            _definition(
                name="glob",
                description=(
                    "Find files by a glob pattern, such as **/*.md, relative to "
                    "an absolute base_path, stopping at limit matches. matches "
                    "are relative to the base_path the result gives; read "
                    "base_path/path. Symlinks "
                    f"are not followed; the search stops after "
                    f"{RIPGREP_TIMEOUT_SECONDS:g} seconds; warning and "
                    "retry_hint say what was left out. "
                    f"{DISCOVERY_IGNORE_NOTE} {notes}"
                ),
                properties={
                    "base_path": _PATH,
                    "pattern": _PATH,
                    "limit": {**_POSITIVE, "maximum": DISCOVERY_RESULT_LIMIT_MAX},
                },
                required=("base_path", "pattern"),
                success=_DISCOVERY_SUCCESS,
                execute=self._executor(GlobToolArgs, self._off_loop(run_glob)),
            ),
            _definition(
                name="grep",
                description=(
                    "Search text files of any size under an absolute base_path "
                    "for a ripgrep regular expression; include_glob, relative "
                    "to base_path, narrows the files. Matching lines come under "
                    "files, one entry per file, as line_number:line; path is "
                    "relative to the base_path the result gives, so read "
                    "base_path/path. A line over "
                    f"{GREP_MAX_LINE_CHARS} characters comes back as an excerpt "
                    "around its first match and a matching binary file is named "
                    "in warning. Output stops at max_matches lines or "
                    f"{GREP_MAX_OUTPUT_BYTES // 1024} KB, and the search after "
                    f"{RIPGREP_TIMEOUT_SECONDS:g} seconds; warning and retry_hint "
                    "say what was left out. "
                    f"{DISCOVERY_IGNORE_NOTE} {notes}"
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
                execute=self._executor(GrepToolArgs, self._off_loop(run_grep)),
            ),
            _definition(
                name=RENDER_PDF_PAGE_TOOL_NAME,
                description=(
                    "Draw pages of a PDF, Word (.docx), PowerPoint (.pptx) or "
                    "Excel (.xlsx) file, by its absolute path, as images to look "
                    "at, where this run can show images: for a scan, a chart, a "
                    "figure, a slide's layout or a table whose layout carries "
                    "the meaning. read still returns the text. Pages are the "
                    "ones read uses; a slide or a sheet number is its page "
                    f"number. At most {MAX_RENDERED_PAGES} pages per call. If "
                    "the result says the viewer is still being set up, continue "
                    "with read. " + notes
                ),
                properties={
                    "path": _PATH,
                    "pages": {
                        "type": "array",
                        "items": _POSITIVE,
                        "minItems": 1,
                        "maxItems": MAX_RENDERED_PAGES,
                    },
                },
                required=("path", "pages"),
                success={"required": ["kind", "path"]},
                execute=self._executor(RenderPdfPageToolArgs, self._render),
                # As in the Action: one call sends up to eight images, and
                # several at once would send dozens.
                concurrency=ToolConcurrency("sequential"),
            ),
        )

    def _executor(
        self, args_model: type[BaseModel], run: _RunCall
    ) -> ReactToolExecutor:
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
                return await run(call, request)
            except BrokerPolicyError as exc:
                details: dict[str, JSONValue] = {}
                if exc.code == PATH_NOT_ABSOLUTE:
                    # A relative path here is often a memory path such as
                    # insights/todos.md.
                    details["fix_hint"] = f"{exc.fix_hint} {self.memory_note}"
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

    def _off_loop(self, body: _Body) -> _RunCall:
        # A read can stream far into a large file and a spill syncs a file to
        # disk, so both stay off the event loop.
        return lambda call, request: asyncio.to_thread(self._run, call, body, request)

    async def _render(self, call: ReactToolCall, request: BaseModel) -> ReactToolResult:
        args = cast(RenderPdfPageToolArgs, request)
        drawn = await draw_document_pages(
            db_path=self.db_path,
            scope=self.scope,
            raw_path=args.path,
            pages=args.pages,
            converted_dir=self.spill_root / _CONVERTED_DIRNAME,
        )
        path = drawn.target.display_path
        if isinstance(drawn, RendererPreparing):
            return ReactToolResult(
                tool_name=call.tool_name,
                status="success",
                output={
                    "kind": RENDERER_PREPARING_OUTPUT_KIND,
                    "path": path,
                    "message": (
                        f"Pages of {path} cannot be drawn yet: the viewer for "
                        f"this format is still being set up. {CONTINUE_WITH_READ}"
                    ),
                },
            )
        images = await asyncio.to_thread(self._keep_pages, drawn)
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output={
                "kind": "pdf_pages",
                "path": path,
                "page_count": drawn.page_count,
                "message": (
                    f"Drew pages of {path}, which has {drawn.page_count} pages. "
                    + " ".join(
                        f"Page {page.number}: {image.ref}"
                        for page, image in zip(drawn.pages, images, strict=True)
                    )
                ),
            },
            images=images,
        )

    def _keep_pages(self, drawn: DrawnDocument) -> tuple[ToolImage, ...]:
        """Keep each page in the run's folder, the file its image is sent from."""

        images = []
        for page in drawn.pages:
            sha256 = hashlib.sha256(page.payload).hexdigest()
            attachment_id = sha256[:ATTACHMENT_ID_HEX_LENGTH]
            name = f"page-{page.number}-{attachment_id}.png"
            images.append(
                ToolImage(
                    ref=f"{TOOL_ATTACHMENT_REF_PREFIX}{attachment_id}",
                    blob_ref=f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
                    display_path=name,
                    mime_type=RENDERED_PAGE_MEDIA_TYPE,
                    byte_size=len(page.payload),
                    sha256=sha256,
                    workspace_root_path=str(self.spill_root),
                    workspace_relative_path=self._keep(
                        _PAGES_DIRNAME, name, page.payload
                    ),
                )
            )
        return tuple(images)

    def _keep_read_image(self, image: ReadImage) -> ToolImage:
        """Keep the bytes ``read`` returned, which every later request sends.

        Pointing at the file that was read would send whatever it holds by the
        next request, or fail the run once something rewrote or removed it.
        """

        return ToolImage(
            ref=image.ref,
            blob_ref=image.blob_ref,
            display_path=image.display_path,
            mime_type=image.mime_type,
            byte_size=image.byte_size,
            sha256=image.sha256,
            workspace_root_path=str(self.spill_root),
            # The name is the bytes' own, so keeping the same read twice
            # rewrites the same file with the same bytes.
            workspace_relative_path=self._keep(
                _READ_IMAGES_DIRNAME, image.blob_ref, image.payload
            ),
        )

    def _keep(self, dirname: str, name: str, payload: bytes) -> str:
        """Write one image into the run's folder; its path relative to the folder."""

        directory = self.spill_root / dirname
        directory.mkdir(mode=_SPILL_ROOT_MODE, exist_ok=True)
        # Lens runs of one Suggestion share this folder and may keep the same
        # image at once; the file is sent from, so never half-written.
        handle, temp_path = tempfile.mkstemp(prefix=".image-", dir=directory)
        with os.fdopen(handle, "wb") as temp_file:
            temp_file.write(payload)
        os.replace(temp_path, directory / name)
        return f"{dirname}/{name}"

    def _run(
        self, call: ReactToolCall, body: _Body, request: BaseModel
    ) -> ReactToolResult:
        result = body(scope=self.scope, request=request)
        output = result.output
        images: tuple[ToolImage, ...] = ()
        if result.image is not None:
            kept = self._keep_read_image(result.image)
            images = (kept,)
            output = {
                **output,
                "attachments": [
                    {
                        "type": "file",
                        "mime_type": kept.mime_type,
                        "path": kept.display_path,
                        "ref": kept.ref,
                        "byte_size": kept.byte_size,
                    }
                ],
            }
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output=store_run_tool_result(spill_root=self.spill_root, output=output),
            images=images,
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
    # Each only reads, and a spill goes to a file of its own.
    concurrency: ToolConcurrency = ToolConcurrency("parallel"),
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
        concurrency=concurrency,
    )


__all__ = [
    "RENDER_PDF_PAGE_TOOL_NAME",
    "build_read_only_file_tools",
    "build_saved_result_read_tool",
]

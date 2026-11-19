"""render_html_page and save_html_as_pdf for a Suggestion's make stage.

Each call prints in a browser session of its own, opened on the desktop app's
browser engine for the Suggestion's run and closed when the call ends, however
it ends. The page is any HTML file ``read`` may open, and it loads local files
only where ``read`` may. What a run with nobody to ask may do is narrower than an
Action's: the page never reaches the network, as make-stage commands never do,
and the PDF is written only into the artifact folder, below a descriptor opened
without following a link, while the folder is still the one pinned.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from pantaray_agents.local_runtime.descriptor_access import DescriptorPathError
from pantaray_agents.local_runtime.descriptor_write import (
    open_writable_parent_descriptor,
    write_bytes_at_descriptor,
)
from pantaray_agents.local_runtime.tooling.documents import MAX_RENDERED_PAGES
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.websocket.server_messages import (
    BrowserFileTarget,
    BrowserNavigateOperation,
    BrowserPrintOperation,
)
from pantaray_agents.tools.contract import (
    BrokerPolicyError,
    JsonSchema,
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    ToolConcurrency,
    react_tool_response_schema,
    tool_error_response,
)
from pantaray_agents.tools.files.read_only_tools import ReadOnlyFileTools
from pantaray_agents.tools.files.read_target import ReadTarget
from pantaray_agents.tools.files.render_pages import (
    RenderHtmlPageToolArgs,
    SaveHtmlAsPdfToolArgs,
)

from ...runtime.browser_contract import BrowserBlockedRequest, BrowserRelayFailureCode
from ...runtime.browser_relay import (
    BROWSER_RELAY_TIMEOUT_SECONDS,
    BrowserOperationDone,
    BrowserRelayFailed,
    close_browser_session,
    request_browser_operation,
)
from ...runtime.db_execution_context import resolve_local_runtime_db_config
from ...runtime.job_types import SUGGESTION_PROCESS_KIND
from ...runtime.runtime_env import read_local_runtime_db_config
from ...storage.migrations.connection import configure_connection
from ..brokering.action_path_policy import WRITE_PATH_DENIED
from ..brokering.broker_browser import browser_page_policy
from ..brokering.printed_pages import draw_printed_pages, resolve_html_file
from .artifact_patch import (
    PATH_OUTSIDE_FOLDER_ERROR_CODE,
    PathOutsideFolder,
    folder_relative,
)
from .commands import PinnedFolder

RENDER_HTML_PAGE_TOOL_ID = "render_html_page"
SAVE_HTML_AS_PDF_TOOL_ID = "save_html_as_pdf"
_PATH: JsonSchema = {"type": "string", "minLength": 1, "pattern": r"\S"}
_PAGE_RULES = (
    "The page is laid out for print by the Pantaray desktop app's built-in "
    "browser engine, with its print CSS and @page rules (A4 when it sets no "
    "page size). Scripts run, but dialogs and window.print() do nothing. It "
    "loads local files only from where read can, and never anything from the "
    "network, so keep styles, fonts and images inline or in local files; "
    "refused requests are listed in blocked_requests. When the desktop app is "
    "closed nothing answers, and the call fails after about "
    f"{int(BROWSER_RELAY_TIMEOUT_SECONDS)} seconds."
)

BLOCKED_REQUESTS_NOTE: Final = "Some resources were refused; see blocked_requests."
_RETRY_ONCE_HINT: Final = (
    "Try once more; if it fails again, tell the user the page could not be checked."
)
# Only the codes a navigate to a local file and a print can end in have a hint.
_FAILURE_HINTS: Final[dict[BrowserRelayFailureCode, str]] = {
    "BROWSER_NAVIGATION_FAILED": "Check that the file is a complete HTML page.",
    "BROWSER_TIMED_OUT": (
        "The page did not finish loading and printing in time. Remove scripts or "
        "resources that keep it busy, then try again."
    ),
    "BROWSER_RENDERER_CRASHED": (
        "The page crashed the browser engine. Simplify heavy scripts or very "
        "large images, then try again."
    ),
    "BROWSER_ACTION_FAILED": (
        "Check the page's @page and print CSS rules, then try again."
    ),
    "BROWSER_RELAY_TIMED_OUT": _RETRY_ONCE_HINT,
    "BROWSER_INTEGRITY_FAILED": _RETRY_ONCE_HINT,
}
_SUGGESTION_PROCESS_SQL = """
SELECT process_id FROM processes
WHERE user_id = ? AND kind = ? AND suggestion_id = ?
"""

type _Run = Callable[[ReactToolCall, BaseModel], Awaitable[ReactToolResult]]


@dataclass(frozen=True, slots=True)
class _Printed:
    pdf: bytes
    blocked: tuple[BrowserBlockedRequest, ...]


@dataclass(frozen=True, slots=True)
class SuggestionHtmlPages:
    files: ReadOnlyFileTools  # the run's read scope and its own folder
    folder: PinnedFolder
    user_id: str
    run_id: str

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return (
            _definition(
                name=RENDER_HTML_PAGE_TOOL_ID,
                description=(
                    "Print a local .html or .htm file, by the absolute path read "
                    "takes, and look at its pages as images, as the user would "
                    "see them printed. pages lists 1-based page numbers one by "
                    f"one, at most {MAX_RENDERED_PAGES}; null draws pages 1 to "
                    f"{MAX_RENDERED_PAGES}. page_count gives the total. " + _PAGE_RULES
                ),
                properties={
                    "path": _PATH,
                    "pages": {
                        "type": ["array", "null"],
                        "items": {"type": "integer", "minimum": 1},
                        "minItems": 1,
                        "maxItems": MAX_RENDERED_PAGES,
                    },
                },
                success={"required": ["kind", "path", "page_count"]},
                execute=self._executor(RenderHtmlPageToolArgs, self._render),
            ),
            _definition(
                name=SAVE_HTML_AS_PDF_TOOL_ID,
                description=(
                    "Print a local .html or .htm file, by the absolute path read "
                    "takes, and save it as a PDF in this Suggestion's artifact "
                    f"folder, `{self.folder.path}`, and nowhere else: pdf_path "
                    "ends in .pdf and is relative to that folder, in a "
                    "subfolder that already exists; a file there is replaced. "
                    "The pages are the ones render_html_page shows; look at "
                    "the saved file with render_pdf_page. " + _PAGE_RULES
                ),
                properties={"path": _PATH, "pdf_path": _PATH},
                success={
                    "required": ["status", "pdf_path"],
                    "properties": {"status": {"const": "saved"}},
                },
                execute=self._executor(SaveHtmlAsPdfToolArgs, self._save),
            ),
        )

    def _executor(
        self, args_model: type[BaseModel], run: _Run
    ) -> Callable[[ReactToolCall, int], Awaitable[ReactToolResult]]:
        async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
            try:
                request = args_model.model_validate(call.tool_args)
                return await run(call, request)
            except ValidationError as exc:
                return _error(call, "INVALID_ARGUMENTS", str(exc)[:1200])
            except PathOutsideFolder as exc:
                return _error(call, PATH_OUTSIDE_FOLDER_ERROR_CODE, str(exc))
            except BrokerPolicyError as exc:
                return tool_error_response(
                    tool_name=call.tool_name,
                    error_code=exc.code,
                    message=str(exc),
                    details={"fix_hint": exc.fix_hint} if exc.fix_hint else None,
                )
            except (DescriptorPathError, OSError) as exc:
                return _error(
                    call, f"{call.tool_name.upper()}_FAILED", str(exc) or "failed"
                )

        return execute

    async def _print(self, raw_path: str) -> tuple[ReadTarget, _Printed]:
        scope = self.files.scope
        target = resolve_html_file(scope=scope, raw_path=raw_path)
        db_path, busy_timeout_ms = resolve_local_runtime_db_config(
            fallback=read_local_runtime_db_config
        )
        # The run's own process owns the session: it stays live until the job
        # ends, so the sweep never closes the session while this call prints.
        owner = _suggestion_process_id(
            db_path, busy_timeout_ms, user_id=self.user_id, suggestion_id=self.run_id
        )
        if owner is None:
            raise _print_failed(
                target,
                BrowserRelayFailed(
                    code="BROWSER_SESSION_GONE",
                    message="This Suggestion has no run to own a browser.",
                    network=(),
                ),
            )
        # The network is off, as for make-stage commands: the page reaches no host.
        policy = browser_page_policy(scope, allow_network=False)

        async def operate(
            operation: BrowserNavigateOperation | BrowserPrintOperation,
        ) -> BrowserOperationDone:
            outcome = await request_browser_operation(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=self.user_id,
                owner_process_id=owner,
                browser="pantaray",
                user_browser=None,
                operation=operation,
                policy=policy,
            )
            if isinstance(outcome, BrowserRelayFailed):
                raise _print_failed(target, outcome)
            return outcome

        try:
            loaded = await operate(
                BrowserNavigateOperation(
                    action="navigate",
                    target=BrowserFileTarget(kind="file", path=str(target.real_path)),
                )
            )
            printed = await operate(BrowserPrintOperation(action="print"))
        finally:
            await close_browser_session(owner_process_id=owner, browser="pantaray")
        # The relay has verified the PDF a print answer claims.
        assert printed.artifact is not None
        return target, _Printed(
            pdf=printed.artifact,
            blocked=(
                *loaded.result.page_data.blocked,
                *printed.result.page_data.blocked,
            ),
        )

    async def _render(self, call: ReactToolCall, request: BaseModel) -> ReactToolResult:
        assert isinstance(request, RenderHtmlPageToolArgs)
        target, printed = await self._print(request.path)
        drawn, message = await draw_printed_pages(
            pdf=printed.pdf,
            name=target.display_path,
            requested=request.pages,
            converted_dir=self.files.spill_root,
        )
        if printed.blocked:
            message = f"{message} {BLOCKED_REQUESTS_NOTE}"
        images = await asyncio.to_thread(self.files.keep_pages, drawn.pages)
        refs = " ".join(
            f"Page {page.number}: {image.ref}"
            for page, image in zip(drawn.pages, images, strict=True)
        )
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output={
                "kind": "html_pages",
                "path": target.display_path,
                "page_count": drawn.page_count,
                "blocked_requests": _blocked_requests_output(printed),
                "message": f"{message} {refs}".strip(),
            },
            images=images,
        )

    async def _save(self, call: ReactToolCall, request: BaseModel) -> ReactToolResult:
        assert isinstance(request, SaveHtmlAsPdfToolArgs)
        # Refused before printing, so a call that cannot be saved never prints.
        relative = folder_relative(self.folder.path, request.pdf_path)
        target, printed = await self._print(request.path)
        replaced = await asyncio.to_thread(self._write, relative, printed.pdf)
        notes = [f"Saved {target.display_path} as {relative}."]
        if replaced:
            notes.append("It replaced the file that was there.")
        if printed.blocked:
            notes.append(BLOCKED_REQUESTS_NOTE)
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output={
                "status": "saved",
                "pdf_path": relative,
                "blocked_requests": _blocked_requests_output(printed),
                "message": " ".join(notes),
            },
        )

    def _write(self, relative: str, pdf: bytes) -> bool:
        """Write the PDF at ``relative``; True when a file was replaced."""

        # Checked after printing, the step closest to the write.
        if not self.folder.unchanged():
            raise BrokerPolicyError(
                "The artifact folder was replaced, so nothing can be saved.",
                code="ARTIFACT_FOLDER_REPLACED",
            )
        parent, name = open_writable_parent_descriptor(
            root_path=self.folder.path, relative_path=relative, create_missing=False
        )
        try:
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise BrokerPolicyError(
                    f"pdf_path is not a regular file: {relative}",
                    code=WRITE_PATH_DENIED,
                )
            write_bytes_at_descriptor(
                parent_descriptor=parent,
                destination_name=name,
                data=pdf,
                replacement_mode=None,
                exclusive=False,
            )
        finally:
            os.close(parent)
        return existing is not None


def _suggestion_process_id(
    db_path: Path, busy_timeout_ms: int, *, user_id: str, suggestion_id: str
) -> str | None:
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            _SUGGESTION_PROCESS_SQL, (user_id, SUGGESTION_PROCESS_KIND, suggestion_id)
        ).fetchone()
    return None if row is None else str(row[0])


def _blocked_requests_output(printed: _Printed) -> list[JSONValue]:
    return [
        {"reason": blocked.reason, "url": blocked.url} for blocked in printed.blocked
    ]


def _print_failed(target: ReadTarget, failure: BrowserRelayFailed) -> BrokerPolicyError:
    return BrokerPolicyError(
        f"Could not print {target.display_path}: {failure.message}",
        code=failure.code,
        fix_hint=_FAILURE_HINTS.get(failure.code),
    )


def _error(call: ReactToolCall, code: str, message: str) -> ReactToolResult:
    return tool_error_response(
        tool_name=call.tool_name, error_code=code, message=message
    )


def _definition(
    *,
    name: str,
    description: str,
    properties: dict[str, JSONValue],
    success: dict[str, JSONValue],
    execute: Callable[[ReactToolCall, int], Awaitable[ReactToolResult]],
) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description=description,
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(properties),
        },
        response_schema=react_tool_response_schema(
            success_schema={"type": "object", **success}
        ),
        execute=execute,
        # One call prints in the desktop app and may send up to eight images.
        concurrency=ToolConcurrency("sequential"),
    )


__all__ = [
    "BLOCKED_REQUESTS_NOTE",
    "RENDER_HTML_PAGE_TOOL_ID",
    "SAVE_HTML_AS_PDF_TOOL_ID",
    "SuggestionHtmlPages",
]

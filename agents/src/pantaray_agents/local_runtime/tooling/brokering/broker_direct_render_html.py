"""The Action's HTML render tool: a page printed by the desktop app, then drawn.

Electron prints the HTML with its own Chromium (``runtime.html_render_relay``),
so no browser has to be started outside the command sandbox. The PDF it returns
is drawn and stored like ``render_pdf_page``'s. What this module owns is what
the page may load: the same folders ``read`` may open, never Pantaray's private
storage, and the network only when the user lets commands use it.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.runtime.html_render_relay import (
    HtmlRendered,
    HtmlRenderFailed,
    HtmlRenderFailureCode,
    request_html_render,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
)
from pantaray_agents.local_runtime.tooling.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_RENDERED_PAGES,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.read_access import READ_ACCESS_SCOPE_FULL_ACCESS
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.read import open_read_target
from pantaray_agents.tools.files.read_document import READ_DOCUMENT_TOO_LARGE
from pantaray_agents.tools.files.read_scope import ReadScope
from pantaray_agents.tools.files.read_target import ReadTarget, resolve_read_target
from pantaray_agents.tools.files.render_pages import (
    RENDER_FORMAT_UNSUPPORTED,
    draw_pdf,
)

from ..repository.command_network_settings import load_command_network_enabled
from .action_path_policy import read_scope
from .broker_common import BrokerContext, ensure_session_capabilities
from .broker_direct_render_pdf import (
    rendered_documents_dir,
    rendered_pages_outcome,
)
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedRenderHtmlPageRequest

HTML_SUFFIXES: Final = frozenset({".html", ".htm"})
BLOCKED_REQUESTS_NOTE: Final = "Some resources were refused; see blocked_requests."
HTML_PDF_TOO_LARGE: Final[HtmlRenderFailureCode] = "HTML_PDF_TOO_LARGE"
_SMALLER_PAGE_HINT: Final = (
    "Use smaller or fewer images, or split the page into several HTML files."
)
_RETRY_ONCE_HINT: Final = (
    "Try once more; if it fails again, tell the user the page could not be checked."
)
_FAILURE_HINTS: Final[dict[HtmlRenderFailureCode, str]] = {
    "HTML_LOAD_FAILED": (
        "Check that the file is a complete HTML page; blocked requests, if any, "
        "are listed in the message."
    ),
    "HTML_RENDER_TIMED_OUT": (
        "The page did not finish loading and printing in time. Remove scripts or "
        "resources that keep it busy, then try again."
    ),
    "HTML_RENDERER_CRASHED": (
        "The page crashed the browser engine. Simplify heavy scripts or very "
        "large images, then try again."
    ),
    "HTML_PRINT_FAILED": (
        "Check the page's @page and print CSS rules, then try again."
    ),
    HTML_PDF_TOO_LARGE: _SMALLER_PAGE_HINT,
    "HTML_RENDER_RELAY_TIMED_OUT": _RETRY_ONCE_HINT,
    "HTML_RENDER_INTEGRITY_FAILED": _RETRY_ONCE_HINT,
}


async def run_render_html_page_executor(
    *, context: BrokerContext, request: ValidatedRenderHtmlPageRequest
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    user_id = context.execution_session.user_id
    target, rendered = await print_html_file(
        context=context,
        scope=read_scope(context),
        raw_path=request.args.path,
        action_id=request.action_id,
        tool_request_id=request.tool_request_id,
    )
    if len(rendered.pdf) > MAX_DOCUMENT_BYTES:
        raise BrokerPolicyError(
            f"The printed PDF of {target.display_path} is larger than "
            f"{MAX_DOCUMENT_BYTES} bytes, too large to draw",
            code=HTML_PDF_TOO_LARGE,
            fix_hint=_SMALLER_PAGE_HINT,
        )
    requested = request.args.pages
    pages = requested or list(range(1, MAX_RENDERED_PAGES + 1))
    converted_dir = rendered_documents_dir(context)
    converted_dir.mkdir(mode=MANAGED_DIRECTORY_MODE, exist_ok=True)
    handle, pdf_path = tempfile.mkstemp(
        prefix=".printed-", suffix=".pdf", dir=converted_dir
    )
    try:
        with os.fdopen(handle, "wb") as pdf_file:
            pdf_file.write(rendered.pdf)
        drawn = await draw_pdf(
            target=target,
            pdf_path=Path(pdf_path),
            pages=pages,
            drop_pages_past_end=True,
        )
    finally:
        os.unlink(pdf_path)
    notes = [f"Printed {target.display_path} to {drawn.page_count} pages."]
    if requested is not None:
        dropped = [number for number in requested if number > drawn.page_count]
        if dropped:
            notes.append(
                f"Pages past the end, not drawn: {', '.join(map(str, dropped))}."
            )
    elif drawn.page_count > MAX_RENDERED_PAGES:
        notes.append(
            f"Only pages 1 to {MAX_RENDERED_PAGES} were drawn; name later pages "
            "in pages to see them."
        )
    if rendered.blocked_requests:
        notes.append(BLOCKED_REQUESTS_NOTE)
    return rendered_pages_outcome(
        user_id=user_id,
        target=target,
        pages=drawn.pages,
        message=" ".join(notes),
        output={
            "kind": "html_pages",
            "path": target.display_path,
            "page_count": drawn.page_count,
            "blocked_requests": blocked_requests_output(rendered),
        },
    )


async def print_html_file(
    *,
    context: BrokerContext,
    scope: ReadScope,
    raw_path: str,
    action_id: str,
    tool_request_id: str,
) -> tuple[ReadTarget, HtmlRendered]:
    """Print an HTML file ``read`` may open, letting it load what ``read`` may."""

    target = resolve_html_file(scope=scope, raw_path=raw_path)
    user_id = context.execution_session.user_id
    rendered = await request_html_render(
        user_id=user_id,
        action_id=action_id,
        process_id=context.actor_process_id,
        tool_request_id=tool_request_id,
        html_path=target.real_path,
        file_roots=_file_roots(scope),
        private_roots=scope.private_storage.storage_roots,
        private_root_exceptions=scope.private_storage.readable_roots,
        allow_network=load_command_network_enabled(
            db_path=context.db_path,
            busy_timeout_ms=context.busy_timeout_ms,
            user_id=user_id,
        ),
    )
    if isinstance(rendered, HtmlRenderFailed):
        raise _render_failed(target, rendered)
    return target, rendered


def blocked_requests_output(rendered: HtmlRendered) -> list[JSONValue]:
    return [
        {"reason": blocked.reason, "url": blocked.url}
        for blocked in rendered.blocked_requests
    ]


def resolve_html_file(*, scope: ReadScope, raw_path: str) -> ReadTarget:
    """Resolve the regular .html/.htm file ``read`` may open, refusing a huge one."""

    target = resolve_read_target(scope=scope, raw_path=raw_path)
    # The real path's suffix: a link named deck.html that leads to a text file
    # would be shown by the browser engine as text.
    if target.real_path.suffix.lower() not in HTML_SUFFIXES:
        raise BrokerPolicyError(
            f"Not an HTML file: {target.display_path}",
            code=RENDER_FORMAT_UNSUPPORTED,
            fix_hint=(
                "Only an .html or .htm file can be printed here. Draw a PDF or an "
                "Office file with render_pdf_page."
            ),
        )
    descriptor = open_read_target(target)
    try:
        status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(status.st_mode):
        raise BrokerPolicyError(
            f"Not a regular file: {target.display_path}",
            code=RENDER_FORMAT_UNSUPPORTED,
        )
    if status.st_size > MAX_DOCUMENT_BYTES:
        raise BrokerPolicyError(
            f"HTML file is larger than {MAX_DOCUMENT_BYTES} bytes: "
            f"{target.display_path}",
            code=READ_DOCUMENT_TOO_LARGE,
            fix_hint=(
                "Move large inline images (data: URLs) into files next to the "
                "page, or split it into several HTML files."
            ),
        )
    return target


def _file_roots(scope: ReadScope) -> tuple[Path, ...]:
    """The folders the page may load local files from: those read may open.

    The HTML file itself is always under one of them, since read resolved it.
    """

    if scope.read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return (Path("/"),)
    return tuple(
        root.canonical_real_path for root in scope.manifest_roots if root.can_read
    )


def _render_failed(target: ReadTarget, failure: HtmlRenderFailed) -> BrokerPolicyError:
    blocked = "".join(
        f" Refused ({request.reason}): {request.url}"
        for request in failure.blocked_requests
    )
    return BrokerPolicyError(
        f"Could not print {target.display_path}: {failure.message}{blocked}",
        code=failure.code,
        fix_hint=_FAILURE_HINTS[failure.code],
    )


__all__ = [
    "BLOCKED_REQUESTS_NOTE",
    "blocked_requests_output",
    "print_html_file",
    "resolve_html_file",
    "run_render_html_page_executor",
]

"""Printed pages: a page's print layout, drawn so the model can look at it.

The desktop app's browser engine prints the page (``runtime.browser_relay``);
the PDF it returns is drawn like ``render_pdf_page``'s. The Action's ``browser``
``screenshot {printed_pages}`` and a Suggestion's make-stage
``render_html_page`` draw through here, and the make stage resolves the HTML
file it prints here, as ``read`` would open it.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.tooling.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_RENDERED_PAGES,
    RenderedPages,
)
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.read import open_read_target
from pantaray_agents.tools.files.read_document import READ_DOCUMENT_TOO_LARGE
from pantaray_agents.tools.files.read_scope import ReadScope
from pantaray_agents.tools.files.read_target import ReadTarget, resolve_read_target
from pantaray_agents.tools.files.render_pages import (
    RENDER_FORMAT_UNSUPPORTED,
    draw_pdf,
)

HTML_SUFFIXES: Final = frozenset({".html", ".htm"})
HTML_PDF_TOO_LARGE: Final = "HTML_PDF_TOO_LARGE"


async def draw_printed_pages(
    *,
    pdf: bytes,
    name: str,
    requested: list[int] | None,
    converted_dir: Path,
) -> tuple[RenderedPages, str]:
    """Draw the printed PDF's pages, and say what was and was not drawn.

    ``name`` is what the messages call the page; ``requested`` None draws the
    first pages.
    """

    if len(pdf) > MAX_DOCUMENT_BYTES:
        raise BrokerPolicyError(
            f"The printed PDF of {name} is larger than {MAX_DOCUMENT_BYTES} "
            "bytes, too large to draw",
            code=HTML_PDF_TOO_LARGE,
            fix_hint=(
                "Use smaller or fewer images, or split the page into several "
                "HTML files."
            ),
        )
    pages = requested or list(range(1, MAX_RENDERED_PAGES + 1))
    handle, pdf_path = tempfile.mkstemp(
        prefix=".printed-", suffix=".pdf", dir=converted_dir
    )
    try:
        with os.fdopen(handle, "wb") as pdf_file:
            pdf_file.write(pdf)
        drawn = await draw_pdf(
            name=name,
            pdf_path=Path(pdf_path),
            pages=pages,
            drop_pages_past_end=True,
        )
    finally:
        os.unlink(pdf_path)
    notes = [f"Printed {name} to {drawn.page_count} pages."]
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
    return drawn, " ".join(notes)


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


__all__ = [
    "HTML_PDF_TOO_LARGE",
    "draw_printed_pages",
    "resolve_html_file",
]

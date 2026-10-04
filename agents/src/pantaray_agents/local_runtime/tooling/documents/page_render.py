"""Turn a PDF's page numbers back into something the model can look at.

The read tool hands the model a PDF's text layer plus the page numbers its text
layer cannot speak for -- a scan, a figure, a table whose columns carry the
meaning. This is the other half: those numbers, drawn.

The drawing happens in a short-lived child process, never in the helper. The
helper is one process serving the local API, the action worker, up to four
subagents and the periodic jobs; a tool call has no time limit of its own, a
thread that will not return cannot be killed, and PDFium is a C++ library that
is not thread-safe and initialises itself the moment it is imported. A child
can be killed, can be confined, and can crash without taking anything with it.
So nothing the helper imports touches pypdfium2 -- only the worker script does,
and the helper reaches it by starting it.
"""

from __future__ import annotations

import asyncio
import json
from asyncio.subprocess import Process
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .document_model import (
    MAX_DOCUMENT_BYTES,
    DocumentExtractionError,
    DocumentTooLargeError,
    EncryptedDocumentError,
)
from .page_render_sandbox import sandboxed_argv

WORKER_SCRIPT: Final = Path(__file__).with_name("pdf_render_worker.py")
RENDERED_PAGE_MEDIA_TYPE: Final = "image/webp"
# What the providers charge for and accept for a single image. A4 portrait
# lands at about 134 dpi, which keeps body text legible without paying for a
# resolution the image is downscaled out of again on the way to the model.
RENDERED_PAGE_LONG_EDGE_PX: Final = 1568
# Each page is a separate image in the same request, so this is a cost and
# context limit rather than a technical one. Measured: eight pages of a dense
# two-column paper draw and encode in 2.9 s.
MAX_RENDERED_PAGES: Final = 8
# Well above what any page needs -- the densest measured page encodes to 300 KiB
# -- and below what a page bounded by the long edge above could ever reach, so a
# worker that has stopped telling the truth about its own output cannot grow the
# helper's memory without bound.
MAX_RENDERED_PAGE_BYTES: Final = 8 * 1024 * 1024
# The same budget the text extractor gets. Nothing outside the renderer bounds
# it: the runtime applies a tool's declared timeout only to calls that share a
# turn, and this tool never shares one, so this is the limit a slow PDF meets.
MAX_PAGE_RENDER_SECONDS: Final = 20.0
# PDFium's reason for refusing a document, as relayed by a process that was
# holding an untrusted file when it said it.
_MAX_REASON_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One page of a PDF, drawn as ``RENDERED_PAGE_MEDIA_TYPE``."""

    number: int
    width_px: int
    height_px: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class RenderedPages:
    """The pages drawn, and how many the document has in all."""

    page_count: int
    pages: tuple[RenderedPage, ...]


class PageOutOfRangeError(DocumentExtractionError):
    """A page was asked for that the document does not have."""

    def __init__(self, *, number: int, total_units: int) -> None:
        super().__init__(f"page {number} is past the document's {total_units} pages")
        self.total_units = total_units


class PageRenderTimeoutError(DocumentExtractionError):
    """The renderer was still working when its time ran out, and was killed."""


async def render_pdf_pages(
    *,
    pdf_path: Path,
    pages: Sequence[int],
    python_executable: Path,
    timeout_seconds: float = MAX_PAGE_RENDER_SECONDS,
) -> RenderedPages:
    """Draw ``pages`` of one PDF, in the order given, one image each.

    ``pages`` are 1-based, as the read tool numbers them, and at most
    ``MAX_RENDERED_PAGES`` of them. ``python_executable`` is the interpreter the
    caller has already established is the one the app ships. ``pdf_path`` is the
    resolved real path the read policy authorized: the sandbox admits the file
    by the name the kernel walks, so one reached through a symlink (``/var`` on
    macOS) is refused as unreadable.

    Raises:
        DocumentTooLargeError: the file is past the size this tool reads.
        EncryptedDocumentError: the PDF cannot be opened without a password.
        PageOutOfRangeError: a page number is past the end of the document.
        PageRenderTimeoutError: the renderer ran out of time and was killed.
        DocumentExtractionError: the PDF is unreadable, or the renderer died.
    """

    size_bytes = pdf_path.stat().st_size
    if size_bytes > MAX_DOCUMENT_BYTES:
        raise DocumentTooLargeError(
            f"document is larger than {MAX_DOCUMENT_BYTES} bytes"
        )
    request = json.dumps(
        {
            "path": str(pdf_path),
            "pages": list(pages),
            "long_edge_px": RENDERED_PAGE_LONG_EDGE_PX,
        }
    ).encode("utf-8")
    process = await asyncio.create_subprocess_exec(
        *sandboxed_argv(
            # -I so the worker takes nothing from the environment or the
            # directory it happens to sit in, -B so it never tries to write a
            # .pyc into a runtime the sandbox has made read-only.
            (str(python_executable), "-I", "-B", str(WORKER_SCRIPT)),
            read_files=(WORKER_SCRIPT, pdf_path),
            python_executable=python_executable,
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        # The worker says everything it has to say on stdout. Its stderr would
        # only carry a traceback from a process holding an untrusted file, and
        # a pipe nobody drains is a way to wedge it.
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(
            _exchange(process, request=request, pages=pages), timeout_seconds
        )
    except TimeoutError as error:
        raise PageRenderTimeoutError(
            f"rendering did not finish within {timeout_seconds:g} seconds"
        ) from error
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _exchange(
    process: Process, *, request: bytes, pages: Sequence[int]
) -> RenderedPages:
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(request + b"\n")
        await process.stdin.drain()
        process.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        # The worker was gone before it could be asked; what happened to it is
        # read off its exit status below, which says more than the broken pipe.
        pass
    line = await process.stdout.readline()
    if not line:
        await process.wait()
        raise DocumentExtractionError(_died(process.returncode))
    page_count, headers = _page_headers(line, pages=pages)
    declared = sum(header.byte_size for header in headers)
    if not 0 <= declared <= MAX_RENDERED_PAGES * MAX_RENDERED_PAGE_BYTES:
        raise DocumentExtractionError(
            f"page renderer declared {declared} bytes of images, which is not a "
            "length this many pages of this size can have"
        )
    try:
        payload = await process.stdout.readexactly(declared)
        if await process.stdout.read(1):
            raise DocumentExtractionError(
                "page renderer sent more image bytes than it declared"
            )
    except asyncio.IncompleteReadError as error:
        raise DocumentExtractionError(
            f"page renderer sent {len(error.partial)} image bytes of {declared}"
        ) from error
    return RenderedPages(page_count=page_count, pages=tuple(_split(headers, payload)))


def _split(headers: Sequence[_PageHeader], payload: bytes) -> list[RenderedPage]:
    pages: list[RenderedPage] = []
    start = 0
    for header in headers:
        end = start + header.byte_size
        pages.append(
            RenderedPage(
                number=header.number,
                width_px=header.width_px,
                height_px=header.height_px,
                payload=payload[start:end],
            )
        )
        start = end
    return pages


@dataclass(frozen=True, slots=True)
class _PageHeader:
    number: int
    width_px: int
    height_px: int
    byte_size: int


def _page_headers(
    line: bytes, *, pages: Sequence[int]
) -> tuple[int, tuple[_PageHeader, ...]]:
    """The worker's one reply line, as the page count and table or as the failure.

    The worker is the process that ran an untrusted file through a C++ parser,
    so this reads its reply rather than trusting it: a password and a page past
    the end are the two outcomes it may name, anything else it could say is an
    unreadable document, and the pages it claims to have drawn have to be the
    ones it was asked for.
    """

    try:
        reply = json.loads(line)
        status = str(reply["status"])
        if status == "password_required":
            raise EncryptedDocumentError("PDF needs a password to open")
        if status == "page_out_of_range":
            raise PageOutOfRangeError(
                number=int(reply["number"]), total_units=int(reply["total_units"])
            )
        if status != "rendered":
            raise DocumentExtractionError(
                f"not a readable pdf file: {str(reply['reason'])[:_MAX_REASON_CHARS]}"
            )
        headers = tuple(
            _PageHeader(
                number=int(page["number"]),
                width_px=int(page["width_px"]),
                height_px=int(page["height_px"]),
                byte_size=int(page["byte_size"]),
            )
            for page in reply["pages"]
        )
        page_count = int(reply["total_units"])
    except (KeyError, TypeError, ValueError) as error:
        raise DocumentExtractionError(
            f"page renderer sent a reply that cannot be read: {error}"
        ) from error
    if tuple(header.number for header in headers) != tuple(pages):
        raise DocumentExtractionError(
            "page renderer returned pages other than the ones it was asked for"
        )
    return page_count, headers


def _died(returncode: int | None) -> str:
    if returncode is not None and returncode < 0:
        return f"page renderer was killed by signal {-returncode} before it replied"
    return f"page renderer exited with status {returncode} before it replied"


__all__ = [
    "MAX_PAGE_RENDER_SECONDS",
    "MAX_RENDERED_PAGES",
    "RENDERED_PAGE_LONG_EDGE_PX",
    "RENDERED_PAGE_MEDIA_TYPE",
    "PageOutOfRangeError",
    "PageRenderTimeoutError",
    "RenderedPage",
    "RenderedPages",
    "render_pdf_pages",
]

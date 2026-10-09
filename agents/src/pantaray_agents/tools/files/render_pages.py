"""Draw pages of a PDF or Office file a read scope reaches, for any caller.

``documents.page_render`` does the drawing, in a child process that holds the
untrusted file; a Word, PowerPoint or Excel file is first laid out as a PDF by
``documents.office_convert`` and kept under its digest in a folder the caller
names. What this module owns is which file may be opened and that it really is
one of those formats. Where the drawn pages are kept is the caller's: the
Action stores them with its history, a shared run in its own folder.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pantaray_agents.local_runtime.runtime.office_runtime import (
    OFFICE_RUNTIME,
    OfficeRuntimeReady,
    OfficeRuntimeSnapshot,
    OfficeRuntimeUnavailable,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
    resolve_local_runtime_storage_base,
)
from pantaray_agents.local_runtime.tooling.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_RENDERED_PAGES,
    DocumentExtractionError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    PageOutOfRangeError,
    PageRenderTimeoutError,
    RenderedPage,
    RenderedPages,
    render_pdf_pages,
)
from pantaray_agents.local_runtime.tooling.documents.office_convert import (
    OfficeConversionTimeoutError,
    OfficeFormat,
    convert_office_to_pdf,
)
from pantaray_agents.tools.contract import BrokerPolicyError

from .read import SAMPLE_BYTES, open_read_target, read_leading_bytes
from .read_document import (
    READ_DOCUMENT_TOO_LARGE,
    READ_DOCUMENT_TOO_LARGE_FIX_HINT,
    READ_DOCUMENT_UNREADABLE_FIX_HINT,
    document_format,
)
from .read_scope import ReadScope
from .read_target import ReadTarget, resolve_read_target

RENDER_CONVERTER_UNAVAILABLE = "RENDER_CONVERTER_UNAVAILABLE"
RENDER_DOCUMENT_ENCRYPTED = "RENDER_DOCUMENT_ENCRYPTED"
RENDER_DOCUMENT_UNREADABLE = "RENDER_DOCUMENT_UNREADABLE"
RENDER_FORMAT_UNSUPPORTED = "RENDER_FORMAT_UNSUPPORTED"
RENDER_PAGE_OUT_OF_RANGE = "RENDER_PAGE_OUT_OF_RANGE"
RENDER_TIMED_OUT = "RENDER_TIMED_OUT"
CONTINUE_WITH_READ = "Continue with read, which returns the file's text."


class RenderPdfPageToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, pattern=r"\S")
    pages: list[Annotated[int, Field(ge=1)]] = Field(
        min_length=1, max_length=MAX_RENDERED_PAGES
    )

    @field_validator("pages")
    @classmethod
    def _reject_repeated_pages(cls, pages: list[int]) -> list[int]:
        # A repeat would spend one of the few page slots on an image the
        # caller already has, so it is a mistake to report rather than honour.
        if len(set(pages)) != len(pages):
            raise ValueError("pages must not name the same page twice")
        return pages


@dataclass(frozen=True, slots=True)
class DrawnDocument:
    """The pages drawn of one file the read scope reached."""

    target: ReadTarget
    page_count: int
    pages: tuple[RenderedPage, ...]


@dataclass(frozen=True, slots=True)
class RendererPreparing:
    """No pages yet: the Office converter is still being installed."""

    target: ReadTarget


async def draw_document_pages(
    *,
    db_path: Path,
    scope: ReadScope,
    raw_path: str,
    pages: list[int],
    converted_dir: Path,
) -> DrawnDocument | RendererPreparing:
    """Draw pages of a PDF or Office file the scope may read.

    An Office file is laid out as a PDF first and kept in ``converted_dir``
    under its bytes' digest, so the same file converts once there. Refusals are
    ``BrokerPolicyError``s with a code and a fix hint.
    """

    target = resolve_read_target(scope=scope, raw_path=raw_path)
    descriptor = open_read_target(target)
    try:
        renderable = _renderable_format(target, descriptor)
        if renderable == "pdf":
            pdf_path = target.real_path
        else:
            office_pdf = await _office_pdf(
                db_path=db_path,
                converted_dir=converted_dir,
                target=target,
                descriptor=descriptor,
                office_format=renderable,
            )
            if office_pdf is None:
                return RendererPreparing(target)
            pdf_path = office_pdf
    finally:
        os.close(descriptor)
    drawn = await _draw(target=target, pdf_path=pdf_path, pages=pages)
    return DrawnDocument(
        target=target, page_count=drawn.page_count, pages=tuple(drawn.pages)
    )


def _renderable_format(
    target: ReadTarget, descriptor: int
) -> Literal["pdf"] | OfficeFormat:
    """Name the format to draw, refusing anything that cannot be, before any work.

    The same recognition the read tool uses, so a PDF saved without an
    extension is still drawn and a notebook never reaches a converter.
    """

    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise _unsupported_format(target)
    sample = read_leading_bytes(descriptor, target=target, limit=SAMPLE_BYTES)
    match document_format(filepath=target.real_path, sample=sample):
        case "pdf":
            return "pdf"
        case "docx" | "pptx" | "xlsx" as office_format:
            return office_format
        case _:
            raise _unsupported_format(target)


def _unsupported_format(target: ReadTarget) -> BrokerPolicyError:
    return BrokerPolicyError(
        f"Cannot draw pages of this file: {target.display_path}",
        code=RENDER_FORMAT_UNSUPPORTED,
        fix_hint=(
            "Only a PDF, Word (.docx), PowerPoint (.pptx) or Excel (.xlsx) file "
            "can be drawn here. Read other documents with read, which returns "
            "their text; read shows an image file as itself."
        ),
    )


async def _office_renderer(db_path: Path) -> Path | None:
    """The LibreOffice.app to convert with, or None while it is being installed.

    Discovery can wait on Spotlight, so it runs on a worker thread.

    Raises:
        BrokerPolicyError: the last install failed; a new attempt has started.
    """

    storage_base = resolve_local_runtime_storage_base(db_path=db_path)
    state = await asyncio.to_thread(_office_runtime_state, storage_base)
    if isinstance(state, OfficeRuntimeUnavailable):
        raise BrokerPolicyError(
            f"Pages of this file cannot be drawn right now ({state.reason})",
            code=RENDER_CONVERTER_UNAVAILABLE,
            fix_hint=f"{CONTINUE_WITH_READ} Drawing may work later.",
        )
    if isinstance(state, OfficeRuntimeReady):
        # The converter's sandbox admits the app by its resolved real path.
        return state.bundle_path.resolve()
    return None


def _office_runtime_state(storage_base: Path) -> OfficeRuntimeSnapshot:
    """The runtime's state, with a failed install reported before it is retried.

    ``ensure_available`` starts a fresh install after a failure and reports it
    as preparing, so the failure is read first: this call says the renderer is
    unavailable, and a later call finds the new attempt preparing or ready.
    """

    before = OFFICE_RUNTIME.snapshot()
    ensured = OFFICE_RUNTIME.ensure_available(storage_base)
    return before if isinstance(before, OfficeRuntimeUnavailable) else ensured


async def _office_pdf(
    *,
    db_path: Path,
    converted_dir: Path,
    target: ReadTarget,
    descriptor: int,
    office_format: OfficeFormat,
) -> Path | None:
    """The PDF of one Office file, converted on the first call and kept after.

    A kept PDF is drawn without asking for LibreOffice at all, whatever state
    its install is in. None when there is none yet and LibreOffice is still
    being installed.
    """

    converted_dir.mkdir(mode=MANAGED_DIRECTORY_MODE, exist_ok=True)
    payload = read_leading_bytes(
        descriptor, target=target, limit=MAX_DOCUMENT_BYTES + 1
    )
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise _too_large(target, f"larger than {MAX_DOCUMENT_BYTES} bytes")
    cached = converted_dir / f"{hashlib.sha256(payload).hexdigest()}.pdf"
    if cached.is_file():
        return cached
    libreoffice_app = await _office_renderer(db_path)
    if libreoffice_app is None:
        return None
    # LibreOffice converts a copy of the bytes just hashed, so the PDF kept
    # under the digest is theirs however the file changes meanwhile.
    handle, source = tempfile.mkstemp(
        prefix=".source-", suffix=f".{office_format}", dir=converted_dir
    )
    try:
        with os.fdopen(handle, "wb") as copy:
            copy.write(payload)
        await convert_office_to_pdf(
            db_path=db_path,
            libreoffice_app=libreoffice_app,
            source=Path(source),
            document_format=office_format,
            destination=cached,
        )
    except OfficeConversionTimeoutError as exc:
        raise BrokerPolicyError(
            f"Laying out {target.display_path} ran out of time: {exc}",
            code=RENDER_TIMED_OUT,
            fix_hint=CONTINUE_WITH_READ,
        ) from exc
    except DocumentExtractionError as exc:
        raise _unreadable(target, exc) from exc
    finally:
        os.unlink(source)
    return cached


async def _draw(
    *, target: ReadTarget, pdf_path: Path, pages: list[int]
) -> RenderedPages:
    try:
        return await render_pdf_pages(
            # The resolved real path: the renderer's sandbox admits the file by
            # the name the kernel walks, not by the one the caller typed.
            pdf_path=pdf_path,
            pages=pages,
            # The interpreter this helper is running on, by the name it was
            # started under: in the app that is the bundled runtime, and in a
            # development virtualenv it is the link that carries the
            # environment's packages, which its resolved target does not.
            python_executable=Path(sys.executable),
        )
    except EncryptedDocumentError as exc:
        raise BrokerPolicyError(
            f"Cannot draw a protected PDF: {target.display_path}: {exc}",
            code=RENDER_DOCUMENT_ENCRYPTED,
            fix_hint=(
                "Open the file in its application, remove the password, and "
                "save an unprotected copy to draw."
            ),
        ) from exc
    except DocumentTooLargeError as exc:
        raise _too_large(target, exc) from exc
    except PageOutOfRangeError as exc:
        raise BrokerPolicyError(
            f"{target.display_path} has {exc.total_units} pages: {exc}",
            code=RENDER_PAGE_OUT_OF_RANGE,
            fix_hint=f"Ask for page numbers between 1 and {exc.total_units}.",
        ) from exc
    except PageRenderTimeoutError as exc:
        raise BrokerPolicyError(
            f"Drawing {target.display_path} ran out of time: {exc}",
            code=RENDER_TIMED_OUT,
            fix_hint="Ask for fewer pages in one call, then call again.",
        ) from exc
    except DocumentExtractionError as exc:
        raise _unreadable(target, exc) from exc


def _unreadable(target: ReadTarget, reason: object) -> BrokerPolicyError:
    return BrokerPolicyError(
        f"Cannot draw pages of {target.display_path}: {reason}",
        code=RENDER_DOCUMENT_UNREADABLE,
        fix_hint=READ_DOCUMENT_UNREADABLE_FIX_HINT,
    )


def _too_large(target: ReadTarget, reason: object) -> BrokerPolicyError:
    return BrokerPolicyError(
        f"Document is too large to draw: {target.display_path}: {reason}",
        code=READ_DOCUMENT_TOO_LARGE,
        fix_hint=READ_DOCUMENT_TOO_LARGE_FIX_HINT,
    )


__all__ = [
    "CONTINUE_WITH_READ",
    "DrawnDocument",
    "RenderPdfPageToolArgs",
    "RendererPreparing",
    "draw_document_pages",
]

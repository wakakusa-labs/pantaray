"""The render tool: a document's pages, drawn, so the model can look at them.

``documents.page_render`` does the drawing, in a child process that holds the
untrusted file. A Word, PowerPoint or Excel file is first laid out as a PDF by
``documents.office_convert``, and that PDF is kept with the Action so the same
file is converted once. What this module owns is the policy around it: which
file may be opened, that it really is one of those formats, and where the
images are kept so the conversation page and the next turn can both find them
again. The bytes ride the attachment path ``capture_screen`` puts a screenshot
on, never the output, which is durable history the memory tools read.
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
from typing import Final, Literal, cast

from pantaray_agents.local_runtime.runtime.local_image_store import (
    write_local_image_blob,
)
from pantaray_agents.local_runtime.runtime.office_runtime import (
    OFFICE_RUNTIME,
    OfficeRuntimeReady,
    OfficeRuntimeSnapshot,
    OfficeRuntimeUnavailable,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
    resolve_action_storage_paths,
    resolve_local_runtime_storage_base,
)
from pantaray_agents.local_runtime.tooling.documents import (
    MAX_DOCUMENT_BYTES,
    RENDERED_PAGE_MEDIA_TYPE,
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
from pantaray_agents.schema.action_conversation import RENDERER_PREPARING_OUTPUT_KIND
from pantaray_agents.schema.agent.base import JSONValue

from .attachment_reference import (
    ATTACHMENT_BLOB_REF_PREFIX,
    ATTACHMENT_ID_HEX_LENGTH,
    TOOL_ATTACHMENT_REF_PREFIX,
)
from .broker_common import (
    BrokerContext,
    BrokerPolicyError,
    ensure_session_capabilities,
)
from .broker_direct_read import SAMPLE_BYTES, open_read_target, read_leading_bytes
from .broker_direct_read_document import (
    READ_DOCUMENT_TOO_LARGE,
    READ_DOCUMENT_TOO_LARGE_FIX_HINT,
    READ_DOCUMENT_UNREADABLE_FIX_HINT,
    document_format,
)
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedRenderPdfPageRequest
from .read_path_resolver import ReadTarget, action_reference_paths, resolve_read_target

RENDER_CONVERTER_UNAVAILABLE = "RENDER_CONVERTER_UNAVAILABLE"
RENDER_DOCUMENT_ENCRYPTED = "RENDER_DOCUMENT_ENCRYPTED"
RENDER_DOCUMENT_UNREADABLE = "RENDER_DOCUMENT_UNREADABLE"
RENDER_FORMAT_UNSUPPORTED = "RENDER_FORMAT_UNSUPPORTED"
RENDER_PAGE_OUT_OF_RANGE = "RENDER_PAGE_OUT_OF_RANGE"
RENDER_TIMED_OUT = "RENDER_TIMED_OUT"
# Under the Action's root, beside its workspace rather than in it, so deleting
# the conversation removes the converted PDFs and no tool of the model's can
# reach or replace one.
RENDERED_DOCUMENTS_DIRNAME: Final = "rendered_documents"
_CONTINUE_WITH_READ = "Continue with read, which returns the file's text."


@dataclass(frozen=True, slots=True)
class _StoredPage:
    """One drawn page, once it has a name and a place to live."""

    number: int
    display_path: str
    ref: str
    blob_ref: str
    storage_path: str
    byte_size: int
    sha256: str


async def run_render_pdf_page_executor(
    *, context: BrokerContext, request: ValidatedRenderPdfPageRequest
) -> UnprojectedBrokerToolOutcome:
    ensure_session_capabilities(context=context)
    target = resolve_read_target(context=context, raw_path=request.path)
    descriptor = open_read_target(target)
    try:
        renderable = _renderable_format(target, descriptor)
        if renderable == "pdf":
            pdf_path = target.real_path
        else:
            office_pdf = await _office_pdf(
                context=context,
                target=target,
                descriptor=descriptor,
                office_format=renderable,
            )
            if office_pdf is None:
                return _preparing(target)
            pdf_path = office_pdf
    finally:
        os.close(descriptor)
    drawn = await _draw(target=target, pdf_path=pdf_path, pages=request.pages)
    stored = [
        _store(user_id=context.execution_session.user_id, page=page)
        for page in drawn.pages
    ]
    output: dict[str, JSONValue] = {
        "kind": "pdf_pages",
        "path": target.display_path,
        # The refs are in the message as well as in the attachments: on the
        # string prompt path, the prompt text says where each image goes.
        "message": (
            f"Drew pages of {target.display_path}, which has "
            f"{drawn.page_count} pages. "
            + " ".join(f"Page {page.number}: {page.ref}" for page in stored)
        ),
        "page_count": drawn.page_count,
        # Durable history keeps this output, so an attachment names the image
        # by its user-scoped storage_path, never by path or by bytes.
        "attachments": [
            {
                "type": "file",
                "source_kind": "local_image_blob",
                "mime_type": RENDERED_PAGE_MEDIA_TYPE,
                "page_number": page.number,
                "path": page.display_path,
                "storage_path": page.storage_path,
                "ref": page.ref,
                "byte_size": page.byte_size,
            }
            for page in stored
        ],
    }
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output,
        search_text=None,
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
        attachments=tuple(
            {
                "type": "file",
                "source_kind": "local_image_blob",
                "ref": page.ref,
                "blob_ref": page.blob_ref,
                "display_path": page.display_path,
                "mime_type": RENDERED_PAGE_MEDIA_TYPE,
                "byte_size": page.byte_size,
                "sha256": page.sha256,
                "storage_path": page.storage_path,
            }
            for page in stored
        ),
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


async def _office_renderer(context: BrokerContext) -> Path | None:
    """The LibreOffice.app to convert with, or None while it is being installed.

    Discovery can wait on Spotlight, so it runs on a worker thread.

    Raises:
        BrokerPolicyError: the last install failed; a new attempt has started.
    """

    storage_base = resolve_local_runtime_storage_base(db_path=context.db_path)
    state = await asyncio.to_thread(_office_runtime_state, storage_base)
    if isinstance(state, OfficeRuntimeUnavailable):
        raise BrokerPolicyError(
            f"Pages of this file cannot be drawn right now ({state.reason})",
            code=RENDER_CONVERTER_UNAVAILABLE,
            fix_hint=f"{_CONTINUE_WITH_READ} Drawing may work later.",
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


def _preparing(target: ReadTarget) -> UnprojectedBrokerToolOutcome:
    """A render that waits on the renderer's install: not a failure, no pages."""

    return UnprojectedBrokerToolOutcome(
        status="success",
        output={
            "kind": RENDERER_PREPARING_OUTPUT_KIND,
            "path": target.display_path,
            "message": (
                f"Pages of {target.display_path} cannot be drawn yet: the "
                "viewer for this format is still being set up. "
                f"{_CONTINUE_WITH_READ} Try drawing the pages again later if "
                "you still need them."
            ),
        },
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
    )


async def _office_pdf(
    *,
    context: BrokerContext,
    target: ReadTarget,
    descriptor: int,
    office_format: OfficeFormat,
) -> Path | None:
    """The PDF of one Office file, converted on the first call and kept after.

    A kept PDF is drawn without asking for LibreOffice at all, whatever state
    its install is in. None when there is none yet and LibreOffice is still
    being installed.
    """

    rendered_documents = (
        resolve_action_storage_paths(
            db_path=context.db_path,
            user_id=context.execution_session.user_id,
            # The render tool is offered to Action runs alone, which always
            # carry their Action.
            action_id=cast(str, context.execution_session.action_id),
        ).action_root
        / RENDERED_DOCUMENTS_DIRNAME
    )
    rendered_documents.mkdir(mode=MANAGED_DIRECTORY_MODE, exist_ok=True)
    payload = read_leading_bytes(
        descriptor, target=target, limit=MAX_DOCUMENT_BYTES + 1
    )
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise _too_large(target, f"larger than {MAX_DOCUMENT_BYTES} bytes")
    cached = rendered_documents / f"{hashlib.sha256(payload).hexdigest()}.pdf"
    if cached.is_file():
        return cached
    libreoffice_app = await _office_renderer(context)
    if libreoffice_app is None:
        return None
    # LibreOffice converts a copy of the bytes just hashed, so the PDF kept
    # under the digest is theirs however the file changes meanwhile.
    handle, source = tempfile.mkstemp(
        prefix=".source-", suffix=f".{office_format}", dir=rendered_documents
    )
    try:
        with os.fdopen(handle, "wb") as copy:
            copy.write(payload)
        await convert_office_to_pdf(
            db_path=context.db_path,
            libreoffice_app=libreoffice_app,
            source=Path(source),
            document_format=office_format,
            destination=cached,
        )
    except OfficeConversionTimeoutError as exc:
        raise BrokerPolicyError(
            f"Laying out {target.display_path} ran out of time: {exc}",
            code=RENDER_TIMED_OUT,
            fix_hint=_CONTINUE_WITH_READ,
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


def _store(*, user_id: str, page: RenderedPage) -> _StoredPage:
    """Name one page and store it, as ``capture_screen`` does a screenshot."""

    sha256 = hashlib.sha256(page.payload).hexdigest()
    attachment_id = sha256[:ATTACHMENT_ID_HEX_LENGTH]
    blob = write_local_image_blob(
        user_id=user_id, payload=page.payload, mime_type=RENDERED_PAGE_MEDIA_TYPE
    )
    return _StoredPage(
        number=page.number,
        display_path=(
            f"page-{page.number}-{attachment_id}{Path(blob.storage_path).suffix}"
        ),
        ref=f"{TOOL_ATTACHMENT_REF_PREFIX}{attachment_id}",
        blob_ref=f"{ATTACHMENT_BLOB_REF_PREFIX}{attachment_id}",
        storage_path=blob.storage_path,
        byte_size=len(page.payload),
        sha256=sha256,
    )


__all__ = ["run_render_pdf_page_executor"]

"""Read a document as text plus the material needed to judge what it left out."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from io import BytesIO
from typing import BinaryIO, Final
from xml.etree.ElementTree import ParseError

from docx.exceptions import PythonDocxError
from docx.opc.exceptions import OpcError
from lxml.etree import XMLSyntaxError
from pptx.exc import PythonPptxError
from pypdf.errors import LimitReachedError, PdfReadError

from .document_model import (
    DOCUMENT_FORMAT_BY_EXTENSION,
    LEGACY_DOCUMENT_REPLACEMENT,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_EXPANDED_BYTES,
    MAX_DOCUMENT_TEXT_CHARS,
    MAX_NOTEBOOK_OUTPUT_CHARS,
    MAX_SHEET_ROWS,
    DocumentExtractionError,
    DocumentFormat,
    DocumentTooLargeError,
    DocumentUnitOutOfRangeError,
    EncryptedDocumentError,
    ExtractedDocument,
)
from .notebook import extract_ipynb
from .page_render import (
    MAX_RENDERED_PAGES,
    RENDERED_PAGE_MEDIA_TYPE,
    PageOutOfRangeError,
    PageRenderTimeoutError,
    RenderedPage,
    RenderedPages,
    render_pdf_pages,
)
from .pdf import extract_pdf
from .presentation import extract_pptx
from .spreadsheet import extract_xlsx
from .word import extract_docx

# Office writes a password-protected document as an OLE2 compound file, not a
# zip, whatever its extension says.
_OLE2_MAGIC: Final = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_EXTRACTORS: Final[
    dict[DocumentFormat, Callable[[BinaryIO, int | None], ExtractedDocument]]
] = {
    "docx": extract_docx,
    "ipynb": extract_ipynb,
    "pdf": extract_pdf,
    "pptx": extract_pptx,
    "xlsx": extract_xlsx,
}
# The OOXML formats, which are the ones that arrive as a zip of parts. Both the
# OLE2 check and the expansion cap speak about that container, so a format that
# is a plain file goes straight to its extractor.
_ZIP_PACKAGE_FORMATS: Final[frozenset[DocumentFormat]] = frozenset(
    {"docx", "pptx", "xlsx"}
)
# What a document's own library raises for content it cannot make sense of. An
# OOXML library's error types do not cover a truncated archive or a part with
# unusable content: ParseError is expat's, which openpyxl reaches through
# defusedxml; defusedxml refusing to expand a part's entities is a ValueError,
# already covered here, as is python-pptx refusing a package whose main part is
# not a presentation. A notebook that is not UTF-8 or not JSON reaches the same
# ValueError, because both UnicodeDecodeError and JSONDecodeError are one.
# pypdf reports a file it cannot parse as PdfReadError, and a structure past one
# of its own size limits -- a decompression bomb, an oversized font width table
# -- as LimitReachedError.
_MALFORMED_DOCUMENT: Final = (
    KeyError,
    LimitReachedError,
    OpcError,
    OSError,
    ParseError,
    PdfReadError,
    PythonDocxError,
    PythonPptxError,
    ValueError,
    XMLSyntaxError,
    zipfile.BadZipFile,
)


def extract_document(
    *,
    source: BinaryIO,
    document_format: DocumentFormat,
    start_unit: int | None = None,
) -> ExtractedDocument:
    """Render one document as text plus the material needed to judge it.

    The caller owns ``source`` and has already proved it is the file the read
    policy authorized, so no extractor reopens the document by pathname.

    ``start_unit`` is the 1-based unit -- the ``unit_kind`` the format counts --
    the text begins at, so a document longer than the text budget is read in
    windows rather than only from its first unit.

    Raises:
        DocumentExtractionError: the document is malformed, protected, too large
            or otherwise cannot be rendered. Partial content is never returned
            as if it were the whole document.
        DocumentUnitOutOfRangeError: ``start_unit`` is past the document's end.
    """

    extract = _EXTRACTORS[document_format]
    # Bounded so an archive that grew after the caller measured it still cannot
    # be pulled into memory whole.
    payload = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLargeError(
            f"document is larger than {MAX_DOCUMENT_BYTES} bytes"
        )
    try:
        if document_format in _ZIP_PACKAGE_FORMATS:
            _reject_unopenable_package(payload)
        return extract(BytesIO(payload), start_unit)
    except _MALFORMED_DOCUMENT as exc:
        raise DocumentExtractionError(
            f"not a readable {document_format} file: {exc}"
        ) from exc
    except MemoryError as exc:
        # The libraries size some allocations by counts the file declares, so
        # an allocation that fails here was sized by the document rather than
        # by the runtime. Unwinding has already released it, which leaves the
        # runtime able to carry on and the read to be refused like any other
        # document past what this tool will expand.
        raise DocumentTooLargeError(
            f"{document_format} file asks for more memory than can be allocated"
        ) from exc


def _reject_unopenable_package(payload: bytes) -> None:
    """Refuse a zip package this tool will not open, before a library opens it.

    Office writes a password-protected document as an OLE2 compound file rather
    than a zip, whatever its extension says. Otherwise, every extractor hands
    the archive to a library that decompresses each part into memory before any
    text budget applies; ``zipfile`` reads at most the ``file_size`` an entry
    declares, so the declared total bounds that expansion.
    """

    if payload.startswith(_OLE2_MAGIC):
        raise EncryptedDocumentError("document is an OLE2 container, not a zip package")
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        expanded_bytes = sum(entry.file_size for entry in archive.infolist())
    if expanded_bytes > MAX_DOCUMENT_EXPANDED_BYTES:
        raise DocumentTooLargeError(
            f"document parts expand to {expanded_bytes} bytes "
            f"(> {MAX_DOCUMENT_EXPANDED_BYTES} bytes)"
        )


__all__ = [
    "DOCUMENT_FORMAT_BY_EXTENSION",
    "LEGACY_DOCUMENT_REPLACEMENT",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_EXPANDED_BYTES",
    "MAX_DOCUMENT_TEXT_CHARS",
    "MAX_NOTEBOOK_OUTPUT_CHARS",
    "MAX_RENDERED_PAGES",
    "MAX_SHEET_ROWS",
    "RENDERED_PAGE_MEDIA_TYPE",
    "DocumentExtractionError",
    "DocumentFormat",
    "DocumentTooLargeError",
    "DocumentUnitOutOfRangeError",
    "EncryptedDocumentError",
    "ExtractedDocument",
    "PageOutOfRangeError",
    "PageRenderTimeoutError",
    "RenderedPage",
    "RenderedPages",
    "extract_document",
    "render_pdf_pages",
]

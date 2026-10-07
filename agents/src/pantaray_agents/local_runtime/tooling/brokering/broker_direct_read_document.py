"""The read tool's document branch: extracted text plus what it could not carry.

Documents are returned as text so every provider can consume them, on the same
paging envelope as a text file. The summary beside that text is bounded here
because it competes with the text for one inline output budget.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

from pantaray_agents.local_runtime.tooling.documents import (
    DOCUMENT_FORMAT_BY_EXTENSION,
    LEGACY_DOCUMENT_REPLACEMENT,
    MAX_DOCUMENT_BYTES,
    DocumentExtractionError,
    DocumentFormat,
    DocumentTooLargeError,
    DocumentUnitOutOfRangeError,
    EncryptedDocumentError,
    ExtractedDocument,
    extract_document,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.text_lines import read_text_value_lines

from .broker_direct_read_page import (
    DEFAULT_READ_LIMIT,
    bound_text_page,
    text_page_output,
)
from .broker_outcome import UnprojectedBrokerToolOutcome
from .broker_protocol import ValidatedReadRequest
from .read_path_resolver import ReadTarget, action_reference_paths

_PDF_MAGIC = b"%PDF-"
READ_DOCUMENT_ENCRYPTED = "READ_DOCUMENT_ENCRYPTED"
READ_DOCUMENT_TOO_LARGE = "READ_DOCUMENT_TOO_LARGE"
READ_DOCUMENT_UNREADABLE = "READ_DOCUMENT_UNREADABLE"
READ_DOCUMENT_UNIT_OUT_OF_RANGE = "READ_DOCUMENT_UNIT_OUT_OF_RANGE"
READ_LEGACY_DOCUMENT_UNSUPPORTED = "READ_LEGACY_DOCUMENT_UNSUPPORTED"
DOCUMENT_BUDGET_TRUNCATION_REASON = "document_budget"
READ_DOCUMENT_BUDGET_RETRY_HINT = (
    "Part of this document was left out; notes say what and how to reach it. "
    "No offset or start_unit continues the missing content."
)
READ_DOCUMENT_UNIT_OUT_OF_RANGE_FIX_HINT = (
    "Read this document again with a start_unit inside it, or without "
    "start_unit to read it from the beginning."
)
READ_DOCUMENT_UNREADABLE_FIX_HINT = (
    "Confirm the file is an undamaged document of this format, then read it "
    "again or open it in its application and save a fresh copy."
)
READ_DOCUMENT_TOO_LARGE_FIX_HINT = (
    "Write out only the part you need as a separate file and read that; for a "
    "spreadsheet, save the sheets you need as CSV."
)
# The summary competes with content for one inline output budget, so both how
# many entries it lists and how long each one may be are capped.
MAX_SUMMARY_ENTRIES = 20
MAX_SUMMARY_NOTES = 12
MAX_SUMMARY_LABEL_CHARS = 60
MAX_SUMMARY_NOTE_CHARS = 240


def document_format(*, filepath: Path, sample: bytes) -> DocumentFormat | None:
    """Name the document format, by extension or by what the bytes say it is.

    Extension alone is how every packaged format is recognised, because a .docx
    is a zip like any other. A PDF announces itself in its first bytes, so one
    saved without an extension, or under someone else's, is still read as the
    document it is rather than refused as a binary file.
    """

    if sample.startswith(_PDF_MAGIC):
        return "pdf"
    return DOCUMENT_FORMAT_BY_EXTENSION.get(filepath.suffix.lower())


def reject_legacy_document(target: ReadTarget) -> None:
    suffix = target.real_path.suffix.lower()
    replacement = LEGACY_DOCUMENT_REPLACEMENT.get(suffix)
    if replacement is None:
        return
    raise BrokerPolicyError(
        f"Cannot read {suffix} documents: {target.display_path}",
        code=READ_LEGACY_DOCUMENT_UNSUPPORTED,
        fix_hint=(
            f"Open the file in its application, save it as {replacement}, and "
            "read that file instead."
        ),
    )


def read_document(
    *,
    target: ReadTarget,
    request: ValidatedReadRequest,
    document_format: DocumentFormat,
    descriptor: int,
) -> UnprojectedBrokerToolOutcome:
    document = _extract(
        target=target,
        descriptor=descriptor,
        document_format=document_format,
        start_unit=request.start_unit,
    )
    offset = request.offset or 1
    column = request.column or 1
    result = read_text_value_lines(
        text=document.text,
        offset=offset,
        column=column,
        limit=request.limit or DEFAULT_READ_LIMIT,
    )
    page = text_page_output(
        kind="document",
        path=target.display_path,
        offset=offset,
        column=column,
        result=result,
    )
    if document.content_truncated:
        # An extraction cut is not a page cut, so the envelope has to carry it
        # or the final page would read as a complete document.
        page = {
            **page,
            "truncated": True,
            "truncation_reason": page["truncation_reason"]
            or DOCUMENT_BUDGET_TRUNCATION_REASON,
            "retry_hint": page["retry_hint"] or _budget_retry_hint(document),
        }
    output = bound_text_page(
        {**page, **_document_summary(document, start_unit=request.start_unit or 1)},
        content=result.content,
        offset=offset,
        column=column,
    )
    return UnprojectedBrokerToolOutcome(
        status="success",
        output=output,
        search_text=cast(str, output["content"]),
        file_paths=(target.display_path,),
        file_reference_paths=action_reference_paths(target),
    )


def _budget_retry_hint(document: ExtractedDocument) -> str:
    """How to reach the content this extraction stopped short of."""

    if document.next_start_unit is None:
        return READ_DOCUMENT_BUDGET_RETRY_HINT
    return (
        f"Extraction stopped at {document.unit_kind} {document.next_start_unit} "
        f"of {document.total_units}; read this path again with "
        f"start_unit={document.next_start_unit} to continue."
    )


def _extract(
    *,
    target: ReadTarget,
    descriptor: int,
    document_format: DocumentFormat,
    start_unit: int | None,
) -> ExtractedDocument:
    """Extract from the regular file the read opened and sampled."""

    try:
        file_stat = os.fstat(descriptor)
        if file_stat.st_size > MAX_DOCUMENT_BYTES:
            raise BrokerPolicyError(
                (
                    f"Document is too large to read: {target.display_path} "
                    f"({file_stat.st_size} bytes > {MAX_DOCUMENT_BYTES} bytes)"
                ),
                code=READ_DOCUMENT_TOO_LARGE,
                fix_hint=READ_DOCUMENT_TOO_LARGE_FIX_HINT,
            )
        with open(descriptor, "rb", closefd=False) as source:
            return extract_document(
                source=source,
                document_format=document_format,
                start_unit=start_unit,
            )
    except DocumentUnitOutOfRangeError as exc:
        raise BrokerPolicyError(
            f"Cannot read {target.display_path} from start_unit {start_unit}: {exc}",
            code=READ_DOCUMENT_UNIT_OUT_OF_RANGE,
            fix_hint=READ_DOCUMENT_UNIT_OUT_OF_RANGE_FIX_HINT,
        ) from exc
    except DocumentTooLargeError as exc:
        raise BrokerPolicyError(
            f"Document is too large to read: {target.display_path}: {exc}",
            code=READ_DOCUMENT_TOO_LARGE,
            fix_hint=READ_DOCUMENT_TOO_LARGE_FIX_HINT,
        ) from exc
    except EncryptedDocumentError as exc:
        raise BrokerPolicyError(
            f"Cannot read a protected document: {target.display_path}: {exc}",
            code=READ_DOCUMENT_ENCRYPTED,
            fix_hint=(
                "Open the file in its application, remove the password, and save "
                "an unprotected copy to read."
            ),
        ) from exc
    except DocumentExtractionError as exc:
        raise BrokerPolicyError(
            f"Cannot read document: {target.display_path}: {exc}",
            code=READ_DOCUMENT_UNREADABLE,
            fix_hint=READ_DOCUMENT_UNREADABLE_FIX_HINT,
        ) from exc


def _document_summary(
    document: ExtractedDocument, *, start_unit: int
) -> dict[str, JSONValue]:
    """Bound the summary so metadata leaves room for a page of text beside it."""

    outline, outline_note = _bounded(
        [_short(entry) for entry in document.outline], "outline entries"
    )
    images, image_note = _bounded(
        [
            # ref identifies the image for a later rendering step, so it is kept
            # whole; a shortened package path would name nothing.
            {
                "location": _short(image.location),
                "ref": image.ref,
                "count": image.count,
            }
            for image in document.images
        ],
        "images",
    )
    charts, chart_note = _bounded(
        [
            {"location": _short(chart.location), "title": _short(chart.title)}
            for chart in document.charts
        ],
        "charts",
    )
    pages, page_note = _bounded(
        list(document.pages_without_text), "pages without a text layer"
    )
    notes, note_note = _bounded(
        [_short(note, MAX_SUMMARY_NOTE_CHARS) for note in document.notes],
        "extraction notes",
        limit=MAX_SUMMARY_NOTES,
    )
    return {
        "document_format": document.document_format,
        "unit_kind": document.unit_kind,
        "start_unit": start_unit,
        "total_units": document.total_units,
        "next_start_unit": document.next_start_unit,
        "outline": cast(JSONValue, outline),
        "images": cast(JSONValue, images),
        "charts": cast(JSONValue, charts),
        "pages_without_text": cast(JSONValue, pages),
        "notes": [
            *notes,
            *note_note,
            *outline_note,
            *image_note,
            *chart_note,
            *page_note,
        ],
    }


def _bounded[T](
    entries: list[T], label: str, *, limit: int = MAX_SUMMARY_ENTRIES
) -> tuple[list[T], list[str]]:
    if len(entries) <= limit:
        return entries, []
    return entries[:limit], [
        f"This document has {len(entries)} {label}; the first {limit} are listed."
    ]


def _short(value: str, limit: int = MAX_SUMMARY_LABEL_CHARS) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


__all__ = [
    "document_format",
    "read_document",
    "reject_legacy_document",
]

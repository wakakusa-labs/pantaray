"""Render a PDF as one section per page, plus what its text layer cannot carry.

A PDF stores text as positioned glyph runs rather than as a document tree, so
what is extracted here is what the file says it draws, not what a reader sees.
That is enough to answer most questions about a report, and unlike a PDF handed
over as a binary attachment it reaches every provider and every subagent. Where
it is not enough -- a scanned page, a figure, a table whose columns carry the
meaning -- the page number is the handle a rendering step needs, so every page
is numbered and the pages with no text layer are listed.

Text is extracted in pypdf's default mode rather than its layout mode. Layout
mode aligns a table's columns, but on running prose it pads whitespace into the
middle of words, costs about twice the time and characters for the same content
-- which halves how much of a document fits the text budget -- and carries its
own denial-of-service history. The columns it would recover are exactly what the
notes send to a rendering step instead.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from typing import BinaryIO, Final

from pypdf import PageObject, PasswordType, PdfReader
from pypdf.generic import Destination, DictionaryObject, PdfObject

from .document_model import (
    DocumentImage,
    DocumentTextBudget,
    EncryptedDocumentError,
    ExtractedDocument,
    continuation_note,
    resolve_start_unit,
)

# An outline nests a child level as a list in place. pypdf's own alias for that
# shape stops at two levels, while a PDF may nest as deep as it likes.
type _OutlineTree = Sequence[Destination | _OutlineTree]


class _PastDeadline(BaseException):
    """Raised from inside pypdf's extraction to abandon a page past the deadline.

    A BaseException, because pypdf catches every Exception raised while it
    draws a form and carries on with the next operator.
    """


# pypdf parses one page at a time and nothing bounds how long a single page
# takes; malformed objects turning into very long or endless loops are a steady
# stream of advisories against this parser. A read runs on a thread that cannot
# be killed, and a thread that never returns holds the one action worker slot
# and keeps the process from exiting, so extraction gives up once it has spent
# this long. A real document never reaches it -- 492 pages of a dense standards
# document extract in 9.4 s, and the text budget ends that read after 2.3 s.
# The runtime applies the read tool's declared 30 s only when read-only calls
# share a turn, so for a read called on its own this is the only limit there is.
# The limit is checked between pages and before each operator a page draws,
# which is what stops a page that draws one form thousands of times, since
# pypdf parses the form's stream again on every draw. It cannot interrupt the
# parse of one content stream, which the stream's own size bounds.
MAX_PDF_EXTRACTION_SECONDS: Final = 20.0
# Each note stands alone and stays short, because the read tool's summary caps
# how long a single note may be and silently shortens one that runs past it.
_PDF_NOTES: Final = (
    "This is the text the PDF stores, not what its pages look like. Layout, "
    "table columns, figures, text inside images, annotations, form values and "
    "attached files are not in it, and the extracted order can differ from the "
    "reading order.",
    "A page in pages_without_text carries no text at all, and a page whose "
    "meaning sits in a figure or a table carries too little; either has to be "
    "rendered as an image before it can be read.",
)


def extract_pdf(source: BinaryIO, start_unit: int | None) -> ExtractedDocument:
    # strict=False is pypdf's default and is kept deliberately: real PDFs
    # deviate from the specification constantly, and strict mode turns each
    # deviation into a refusal to read a file every other reader opens.
    reader = PdfReader(source, strict=False)
    _open_without_password(reader)
    deadline = time.monotonic() + MAX_PDF_EXTRACTION_SECONDS
    total_units = len(reader.pages)
    start = resolve_start_unit(start_unit, total_units=total_units, unit_kind="page")
    # Before the pages: the outline covers the whole document however little of
    # it is read here, and it is the one part of the summary that survives a
    # document whose text spends the whole budget.
    outline = tuple(_outline_entries(reader, deadline))
    budget = DocumentTextBudget(
        unit_kind="page", total_units=total_units, start_unit=start
    )
    images: list[DocumentImage] = []
    pages_without_text: list[int] = []
    gave_up_at = 0
    # Indexed rather than iterated: a page before the start is never parsed and
    # never has its text extracted, which is what makes starting late cheap.
    for number in range(start, total_units + 1):
        if time.monotonic() > deadline:
            gave_up_at = number
            break
        page = reader.pages[number - 1]
        try:
            text = page.extract_text(visitor_operand_before=_stop_after(deadline))
        except _PastDeadline:
            gave_up_at = number
            break
        count = _image_count(page)
        if count:
            location = f"page {number}"
            images.append(DocumentImage(location=location, ref=location, count=count))
        if not text.strip():
            pages_without_text.append(number)
        # A page the budget cannot hold ends this read; total_units still counts
        # every page, and next_start_unit says where the rest of them begin.
        if not budget.add_unit(number, f"## Page {number}", *text.splitlines(), ""):
            break
    # A deadline that passed before the first page of this read leaves nowhere
    # to continue from: reading again from the same page would be as slow.
    resumable_at = gave_up_at if gave_up_at > start else 0
    return ExtractedDocument(
        document_format="pdf",
        unit_kind="page",
        total_units=total_units,
        text=budget.finish(),
        content_truncated=budget.stopped or gave_up_at > 0,
        next_start_unit=budget.next_start_unit or resumable_at or None,
        outline=outline,
        images=tuple(images),
        pages_without_text=tuple(pages_without_text),
        notes=(
            *_PDF_NOTES,
            *_time_limit_notes(gave_up_at, total_units, resumable_at),
            *budget.notes(),
        ),
    )


def _stop_after(deadline: float) -> Callable[[object, object, object, object], None]:
    """A pypdf operator visitor that abandons the page once ``deadline`` passes."""

    def check(operator: object, operands: object, cm: object, tm: object) -> None:
        if time.monotonic() > deadline:
            raise _PastDeadline

    return check


def _open_without_password(reader: PdfReader) -> None:
    """Open a PDF that restricts what may be done with it rather than who reads it.

    A PDF whose owner password blocks printing or editing still has an empty
    user password, so it opens with no secret at all. Only one meant to be
    unreadable without a password is refused here.
    """

    if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
        raise EncryptedDocumentError("PDF needs a password to open")


def _outline_entries(reader: PdfReader, deadline: float) -> Iterator[str]:
    """The PDF's bookmarks, nested with '>' and carrying the page each opens."""

    for depth, destination in _outline_items(reader.outline):
        if time.monotonic() > deadline:
            return
        # A title is untrusted text that would otherwise break the line it is
        # written on, or the ones after it.
        title = " ".join((destination.title or "").split())
        if not title:
            continue
        page = reader.get_destination_page_number(destination)
        opens = "" if page is None else f" (page {page + 1})"
        yield f"{'>' * depth} {title}{opens}" if depth else f"{title}{opens}"


def _outline_items(outline: _OutlineTree) -> Iterator[tuple[int, Destination]]:
    for item in outline:
        if isinstance(item, Destination):
            yield 0, item
        else:
            for depth, nested in _outline_items(item):
                yield depth + 1, nested


def _image_count(page: PageObject) -> int:
    """Count a page's image XObjects without decoding one of them.

    ``page.images`` decodes every image to hand back pixels, which is both the
    slow path and the one a malformed image dictionary attacks. All the model
    needs is that an image is there and which page it sits on, and all a later
    rendering step needs is that page number.
    """

    resources = _dictionary(page.get("/Resources"))
    xobjects = None if resources is None else _dictionary(resources.get("/XObject"))
    if xobjects is None:
        return 0
    return sum(
        1
        for name in xobjects
        if (entry := _dictionary(xobjects.raw_get(name))) is not None
        and entry.get("/Subtype") == "/Image"
    )


def _dictionary(value: object) -> DictionaryObject | None:
    """Resolve one untrusted entry to a dictionary, or to nothing at all.

    Every value read out of a PDF may be a reference to somewhere else in the
    file, and may be of any type whatever the key implies, so this is the trust
    boundary for the shapes the image count walks.
    """

    resolved = value.get_object() if isinstance(value, PdfObject) else value
    return resolved if isinstance(resolved, DictionaryObject) else None


def _time_limit_notes(
    gave_up_at: int, total_units: int, resumable_at: int
) -> Iterator[str]:
    if gave_up_at:
        yield (
            f"Extraction stopped at page {gave_up_at} of {total_units} after "
            f"{MAX_PDF_EXTRACTION_SECONDS:.0f} seconds. This PDF is slower to "
            "read than the tool allows; the pages after that point are not in "
            "this result."
        )
    if resumable_at:
        yield continuation_note("page", resumable_at)


__all__ = ["extract_pdf"]

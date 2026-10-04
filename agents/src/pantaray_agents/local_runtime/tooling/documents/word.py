"""Render a .docx body as headings, lists and tables, using python-docx."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import BinaryIO, Final

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tc
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from .document_model import (
    MAX_TABLE_COLUMNS,
    DocumentExtractionError,
    DocumentImage,
    DocumentTextBudget,
    ExtractedDocument,
    pipe_table,
    resolve_start_unit,
    table_cut_notes,
)

_HEADING_STYLE: Final = re.compile(r"heading\s*([1-9])", re.IGNORECASE)
_LIST_STYLE: Final = re.compile(r"list", re.IGNORECASE)
# Each note stands alone and stays short, because the read tool's summary caps
# how long a single note may be and silently shortens one that runs past it.
_DOCX_NOTES: Final = (
    "Formatting, comments, tracked changes, headers, footers, footnotes and "
    "endnotes are not included. Charts and other drawings that are not "
    "embedded pictures are not listed.",
    "List numbering is not resolved, so a numbered item carries the same "
    "marker as a bullet.",
    "Pages are not counted here; render_pdf_page draws the laid-out pages "
    "and reports the page count.",
)


def extract_docx(source: BinaryIO, start_unit: int | None) -> ExtractedDocument:
    document = Document(source)
    blocks = list(_body_blocks(document.element.body))
    total_units = sum(block.tag == qn("w:p") for block in blocks)
    start = resolve_start_unit(
        start_unit, total_units=total_units, unit_kind="paragraph"
    )
    budget = DocumentTextBudget(
        unit_kind="paragraph", total_units=total_units, start_unit=start
    )
    images: list[DocumentImage] = []
    outline: list[str] = []
    paragraphs = 0
    tables = 0
    table_cut = False
    for block in blocks:
        is_paragraph = block.tag == qn("w:p")
        if is_paragraph:
            paragraphs += 1
        else:
            tables += 1
        # A table sits between paragraphs, so it is read as part of the
        # paragraph it follows: a read resuming at a later paragraph skips it
        # because the read that reached that paragraph already carried it.
        number = max(paragraphs, 1)
        if number < start:
            continue
        if is_paragraph:
            location = f"paragraph {paragraphs}"
            line = _paragraph_line(Paragraph(block, document))
            if line.startswith("#"):
                outline.append(line)
            lines = [line]
        else:
            location = f"table {tables}"
            lines, cut = pipe_table(_table_rows(Table(block, document)))
            table_cut = table_cut or cut
        images.extend(_block_images(block, document, location))
        if not budget.add_unit(number, *lines):
            break
    return ExtractedDocument(
        document_format="docx",
        unit_kind="paragraph",
        total_units=total_units,
        text=budget.finish(),
        content_truncated=budget.stopped or table_cut,
        next_start_unit=budget.next_start_unit,
        outline=tuple(outline),
        images=tuple(images),
        notes=(*_DOCX_NOTES, *table_cut_notes(table_cut), *budget.notes()),
    )


def _body_blocks(container: BaseOxmlElement) -> Iterator[BaseOxmlElement]:
    """Paragraphs and tables in reading order, including those a block-level
    content control (``w:sdt``) wraps."""

    for child in container.iterchildren():
        if child.tag in (qn("w:p"), qn("w:tbl")):
            yield child
        elif child.tag == qn("w:sdt"):
            for content in child.iterchildren(qn("w:sdtContent")):
                yield from _body_blocks(content)


def _inline_text(container: BaseOxmlElement) -> str:
    """A paragraph's text as python-docx reads it, plus inline content controls."""

    parts: list[str] = []
    for child in container.iterchildren():
        if child.tag in (qn("w:r"), qn("w:hyperlink")):
            parts.append(child.text or "")
        elif child.tag == qn("w:sdt"):
            for content in child.iterchildren(qn("w:sdtContent")):
                parts.append(_inline_text(content))
    return "".join(parts)


def _paragraph_line(paragraph: Paragraph) -> str:
    text = _inline_text(paragraph._p)
    if not text.strip():
        return ""
    style = (paragraph.style.name or "") if paragraph.style is not None else ""
    heading = _HEADING_STYLE.fullmatch(style)
    if heading is not None:
        return f"{'#' * int(heading.group(1))} {text}"
    if style.casefold() == "title":
        return f"# {text}"
    if _LIST_STYLE.search(style):
        return f"- {text}"
    return text


def _table_rows(table: Table) -> Iterator[list[str]]:
    """Each row's cell text, a cell repeated per grid column it spans, as ``row.cells``.

    ``row.cells`` cannot be handed an untrusted table. It repeats a cell once
    for every grid column the cell declares it spans, so one ``gridSpan`` of
    2**31 - 1 builds a tuple of that length; and it finds the cell a vertical
    merge continues by walking back up the table a row at a time, re-scanning
    each row, which measured 31 s for a 200-row, 10-column merge. The same
    layout is built here in one pass: each row remembers the merge root that
    starts at each grid offset, so the row below finds it directly, and a row
    stops repeating text one column past what a table keeps.
    """

    roots_above: dict[int, CT_Tc] = {}
    for tr in table._tbl.tr_lst:
        roots: dict[int, CT_Tc] = {}
        texts: list[str] = []
        offset = tr.grid_before
        for tc in tr.tc_lst:
            root = tc
            if tc.vMerge == "continue":
                above = roots_above.get(offset)
                if above is None:
                    raise DocumentExtractionError(
                        f"a vertically merged table cell at grid column {offset} "
                        "has no cell above it to continue"
                    )
                root = above
            roots[offset] = root
            # The root's span, not this cell's: a continuing cell repeats the
            # cell it continues, as python-docx lays it out.
            room = MAX_TABLE_COLUMNS + 1 - len(texts)
            if room > 0:
                texts.extend([_Cell(root, table).text] * min(root.grid_span, room))
            offset += tc.grid_span
        roots_above = roots
        yield texts


def _block_images(
    block: BaseOxmlElement, document: DocxDocument, location: str
) -> list[DocumentImage]:
    """Images this block displays, named by the package part they live in."""

    relationships = document.part.rels
    counts: dict[str, int] = {}
    for blip in block.findall(f".//{qn('a:blip')}"):
        reference = blip.get(qn("r:embed"))
        if reference is None or reference not in relationships:
            continue
        relationship = relationships[reference]
        if relationship.is_external:
            continue
        part_name = str(relationship.target_part.partname).lstrip("/")
        counts[part_name] = counts.get(part_name, 0) + 1
    return [
        DocumentImage(location=location, ref=part_name, count=count)
        for part_name, count in counts.items()
    ]


__all__ = ["extract_docx"]

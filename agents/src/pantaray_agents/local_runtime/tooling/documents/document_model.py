"""Shapes and limits shared by the read tool's document extractors.

A document is returned to the model as text, so the model also needs the
judgement material it can no longer infer from the file itself: how many units
the document has, where embedded images and charts sit, and what the extraction
deliberately left out.

Every limit here bounds untrusted input. A document expands far past the bytes
it occupies, so the payload size, the rendered text and each table are capped
while they are expanded rather than trimmed afterwards.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

DocumentFormat = Literal["docx", "ipynb", "pdf", "pptx", "xlsx"]
DocumentUnitKind = Literal["cell", "page", "paragraph", "sheet", "slide"]

MAX_DOCUMENT_BYTES: Final = 20 * 1024 * 1024
# Every part is decompressed into memory before any text budget applies, and XML
# deflates about a thousand to one, so the archive's declared expanded size is
# capped separately from the file's own size. Media is already compressed and
# enters an archive close to its own size, so a genuine document only passes
# this cap when it carries more than 20 MB of XML.
#
# Design limit: python-docx and python-pptx parse each part they open into a
# whole lxml tree, which measured 4 s and 675 MiB for a 39 MiB document body and
# 1 s and 909 MiB for a 38 MiB chart part; this keeps a read to a few seconds
# and under 1 GiB, and nothing outside it bounds a read that is called on its
# own. Revisit with a per-format cap if real documents are reported as
# unreadable at this size.
MAX_DOCUMENT_EXPANDED_BYTES: Final = 40 * 1024 * 1024
MAX_DOCUMENT_TEXT_CHARS: Final = 400_000
MAX_TABLE_ROWS: Final = 500
MAX_TABLE_COLUMNS: Final = 64
# A sheet is a whole document's worth of table, so it is allowed far more than a
# table embedded in prose while still bounding what one unit can expand to.
MAX_SHEET_ROWS: Final = 2_000
MAX_SHEET_COLUMNS: Final = 128
# One stored notebook output is one value the model is judging, not the
# notebook's content, so a training log or a large frame repr cannot spend the
# whole text budget in one cell. This holds a full traceback or a default pandas
# repr, and a hundred outputs still fit inside the text budget.
MAX_NOTEBOOK_OUTPUT_CHARS: Final = 4_000

_MIDNIGHT: Final = datetime.time()

DOCUMENT_FORMAT_BY_EXTENSION: Final[Mapping[str, DocumentFormat]] = MappingProxyType(
    # A .xlsm and a .pptm are the same package as a .xlsx and a .pptx with a
    # macro part beside them, and reading content never runs the macro.
    {
        ".docx": "docx",
        ".ipynb": "ipynb",
        ".pdf": "pdf",
        ".pptx": "pptx",
        ".pptm": "pptx",
        ".xlsx": "xlsx",
        ".xlsm": "xlsx",
    }
)
# Only a legacy format whose modern counterpart this tool already reads: telling
# the model to re-save as a format the tool still refuses would just loop.
LEGACY_DOCUMENT_REPLACEMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        ".doc": ".docx",
        ".odt": ".docx",
        ".odp": ".pptx",
        ".ppt": ".pptx",
        ".xls": ".xlsx",
        ".ods": ".xlsx",
    }
)


class DocumentExtractionError(Exception):
    """A document cannot be rendered as text."""


class DocumentUnitOutOfRangeError(DocumentExtractionError):
    """Extraction was asked to start past the last unit the document has."""


class EncryptedDocumentError(DocumentExtractionError):
    """The document cannot be opened without a password this tool does not have."""


class DocumentTooLargeError(DocumentExtractionError):
    """The document, or what it expands to, is past a size this tool will read."""


@dataclass(frozen=True, slots=True)
class DocumentImage:
    """Embedded images shown at one location, addressable by ``ref``."""

    location: str
    ref: str
    count: int


@dataclass(frozen=True, slots=True)
class DocumentChart:
    """A chart part, which is vector data rather than an embedded image."""

    location: str
    title: str


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """One window of a document as text, and what reading it left out.

    ``total_units`` counts the whole document, while the text, the outline and
    the rest of the judgement material cover the units this extraction reached.
    ``next_start_unit`` is the unit a further extraction must begin at to
    continue, and is None once there is nothing after this window to read.
    """

    document_format: DocumentFormat
    unit_kind: DocumentUnitKind
    total_units: int
    text: str
    content_truncated: bool
    next_start_unit: int | None = None
    outline: tuple[str, ...] = ()
    images: tuple[DocumentImage, ...] = ()
    charts: tuple[DocumentChart, ...] = ()
    pages_without_text: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()


def resolve_start_unit(
    start_unit: int | None, *, total_units: int, unit_kind: DocumentUnitKind
) -> int:
    """The unit extraction begins at, refusing one past the document's end.

    A start past the end is a caller's mistake rather than an empty document, so
    it is reported with the count the caller needed instead of read as nothing.
    """

    if start_unit is None:
        return 1
    if start_unit > total_units:
        raise DocumentUnitOutOfRangeError(
            f"this document has {total_units} {unit_kind}s, so it has no "
            f"{unit_kind} {start_unit}"
        )
    return start_unit


def pipe_table(
    rows: Iterable[Sequence[str]],
    *,
    max_rows: int = MAX_TABLE_ROWS,
    max_columns: int = MAX_TABLE_COLUMNS,
) -> tuple[list[str], bool]:
    """Render table rows as pipe-separated lines, and report whether it was cut.

    ``rows`` is consumed lazily and abandoned at ``max_rows``, so a caller whose
    source is unbounded never has to expand it past the limit set here.
    """

    kept: list[list[str]] = []
    cut = False
    for row in rows:
        if len(kept) >= max_rows:
            cut = True
            break
        cut = cut or len(row) > max_columns
        kept.append([_cell(value) for value in row[:max_columns]])
    if not kept:
        return [], cut
    width = max(len(row) for row in kept)
    lines = [_pipe_row(row, width) for row in kept]
    lines.insert(1, _pipe_row(["---"] * width, width))
    if cut:
        lines.append(f"[table cut to {width} columns and {len(kept)} rows]")
    return lines, cut


def table_cut_notes(cut: bool) -> tuple[str, ...]:
    """The note a cut table needs: no start_unit reaches the cells it dropped."""

    if not cut:
        return ()
    return (
        f"Tables longer than {MAX_TABLE_ROWS} rows or wider than "
        f"{MAX_TABLE_COLUMNS} columns are cut where the text says [table cut ...]; "
        "read cannot reach the rest, so read such a table with run_python.",
    )


def stored_value_text(value: object) -> str:
    """One stored value as the model reads it, not as its application shows it.

    A spreadsheet cell and a chart's saved data point are the same kind of
    thing to a reader: a value the producing application wrote down, whose
    display depends on formatting this tool deliberately leaves out.
    """

    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        # Excel has no date type of its own, so a plain date arrives as a
        # timestamp at midnight.
        if value.time() == _MIDNIGHT:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    return str(value)


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _pipe_row(values: Sequence[str], width: int) -> str:
    padded = [*values[:width], *[""] * (width - len(values))]
    return f"| {' | '.join(padded)} |"


class DocumentTextBudget:
    """Collect rendered units and stop before the text budget is overrun.

    Extractors offer one whole unit at a time, so an untrusted document is never
    decompressed in full only to be trimmed afterwards, and the unit the budget
    stops at is left out of the text entirely: extracting the document again
    from ``next_start_unit`` continues it with nothing repeated and nothing
    skipped. The one exception is a unit longer on its own than the whole
    budget, which no ``start_unit`` could ever reach past: as much of it as fits
    is written and the continuation moves on to the unit after it.
    """

    def __init__(
        self, *, unit_kind: DocumentUnitKind, total_units: int, start_unit: int
    ) -> None:
        self._unit_kind = unit_kind
        self._total_units = total_units
        self._start_unit = start_unit
        self._lines: list[str] = []
        self._used = 0
        self._stopped_at: int | None = None
        self._cut_unit: int | None = None

    def add_unit(self, number: int, *lines: str) -> bool:
        """Write one unit whole, and report whether extraction may continue."""

        if self._fits(lines):
            self._write(lines)
            return True
        if number > self._start_unit:
            self._stopped_at = number
            return False
        # Stopping before the unit this extraction began at would hand back a
        # continuation that repeats the same read forever.
        self._write(lines)
        self._cut_unit = number
        self._stopped_at = number + 1
        return False

    @property
    def stopped(self) -> bool:
        return self._stopped_at is not None

    @property
    def next_start_unit(self) -> int | None:
        if self._stopped_at is None or self._stopped_at > self._total_units:
            return None
        return self._stopped_at

    def finish(self) -> str:
        text = "\n".join(self._lines).strip("\n")
        return f"{text}\n" if text else ""

    def notes(self) -> tuple[str, ...]:
        if self._stopped_at is None:
            return ()
        stop = (
            f"The {self._unit_kind} {self._cut_unit} is longer on its own than "
            f"the {MAX_DOCUMENT_TEXT_CHARS} character budget, so it is cut here "
            "and the rest of it cannot be read as text."
            if self._cut_unit is not None
            else (
                f"Extraction stopped at the {MAX_DOCUMENT_TEXT_CHARS} character "
                f"budget, before {self._unit_kind} {self._stopped_at} of "
                f"{self._total_units}."
            )
        )
        if self.next_start_unit is None:
            return (stop,)
        return (stop, continuation_note(self._unit_kind, self.next_start_unit))

    def _fits(self, lines: Sequence[str]) -> bool:
        used = self._used
        for line in lines:
            if used + len(line) > MAX_DOCUMENT_TEXT_CHARS:
                return False
            used += len(line) + 1
        return True

    def _write(self, lines: Sequence[str]) -> None:
        for line in lines:
            room = MAX_DOCUMENT_TEXT_CHARS - self._used
            if len(line) > room:
                # Only the oversized-unit path reaches a line the budget cannot
                # hold, and leaving it out whole would make that unit unreadable
                # at every start_unit, so the room that is left carries its
                # start. Every other unit was measured to fit before it got here.
                if room > 0:
                    self._lines.append(line[:room])
                return
            self._lines.append(line)
            self._used += len(line) + 1


def continuation_note(unit_kind: DocumentUnitKind, next_start_unit: int) -> str:
    """How to read the rest, said beside the text so paging cannot lose it."""

    return (
        f"Read this document again with start_unit={next_start_unit} to "
        f"continue from that {unit_kind}."
    )


__all__ = [
    "DOCUMENT_FORMAT_BY_EXTENSION",
    "LEGACY_DOCUMENT_REPLACEMENT",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_EXPANDED_BYTES",
    "MAX_DOCUMENT_TEXT_CHARS",
    "MAX_NOTEBOOK_OUTPUT_CHARS",
    "MAX_SHEET_COLUMNS",
    "MAX_SHEET_ROWS",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "DocumentChart",
    "DocumentExtractionError",
    "DocumentFormat",
    "DocumentImage",
    "DocumentTextBudget",
    "DocumentTooLargeError",
    "DocumentUnitKind",
    "DocumentUnitOutOfRangeError",
    "EncryptedDocumentError",
    "ExtractedDocument",
    "continuation_note",
    "pipe_table",
    "resolve_start_unit",
    "table_cut_notes",
    "stored_value_text",
]

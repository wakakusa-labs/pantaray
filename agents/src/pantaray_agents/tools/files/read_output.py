"""The read tool's output variants, on one shared cursor envelope.

A file, an extracted document, a directory listing and an image attachment are
four answers to one call, so they are declared together and closed as one union
that ``read`` returns and that the memory projection reads back.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, RootModel

from .text_encoding import TextEncoding


class ReadTextPageOutput(BaseModel):
    """One page of text on the read tool's cursor envelope.

    File contents and extracted document text are paged the same way, so
    ``offset``, ``column``, ``limit`` and ``next_offset`` mean the same thing in
    both. Each variant declares its own ``kind`` and truncation reasons.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    content: str
    offset: int
    column: int
    end_line: int
    end_column: int
    total_lines: int | None
    next_offset: int | None = None
    next_column: int | None = None
    truncated: bool = False
    retry_hint: str | None = None


class ReadFileOutput(ReadTextPageOutput):
    kind: Literal["file"]
    truncation_reason: Literal["line_count_budget", "page_limit"] | None = None
    encoding: TextEncoding


class ReadDocumentImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    location: str
    ref: str
    count: int


class ReadDocumentChart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    location: str
    title: str


class ReadDocumentOutput(ReadTextPageOutput):
    """Extracted document text, plus what the extraction could not carry.

    ``start_unit`` is the unit the text begins at and ``total_units`` counts the
    whole document, while the other summary fields cover the units this
    extraction reached; all of them repeat unchanged on every page of that text.
    ``document_budget`` means extraction stopped before the end of the document,
    which ``next_start_unit`` continues when there is a way to continue it.
    """

    kind: Literal["document"]
    truncation_reason: (
        Literal["document_budget", "line_count_budget", "page_limit"] | None
    ) = None
    document_format: Literal["docx", "ipynb", "pdf", "pptx", "xlsx"]
    unit_kind: Literal["cell", "page", "paragraph", "sheet", "slide"]
    start_unit: int
    total_units: int
    next_start_unit: int | None = None
    outline: list[str]
    images: list[ReadDocumentImage]
    charts: list[ReadDocumentChart]
    pages_without_text: list[int]
    notes: list[str]


class ReadDirectoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    kind: Literal["file", "directory"]


class ReadDirectoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["directory"]
    path: str
    entries: list[ReadDirectoryEntry]
    offset: int
    next_offset: int | None = None
    truncated: bool = False
    truncation_reason: Literal["page_limit"] | None = None
    retry_hint: str | None = None
    # Entries skipped without being listed: symlinks, unreadable entries.
    warning: str | None = None


class ReadAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: Literal["file"]
    mime_type: str
    path: str
    ref: str
    byte_size: int


class ReadAttachmentOutput(BaseModel):
    """An image, returned as its own bytes because no text stands in for it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["attachment"]
    path: str
    mime_type: str
    message: str
    attachments: list[ReadAttachment]


class ReadToolOutput(
    RootModel[
        ReadFileOutput | ReadDocumentOutput | ReadDirectoryOutput | ReadAttachmentOutput
    ]
):
    model_config = ConfigDict(strict=True)

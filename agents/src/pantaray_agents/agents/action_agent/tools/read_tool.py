"""Brokered workspace read tool definition."""

from __future__ import annotations

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import ReadToolArgs
from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.tools.files.text_lines import (
    MAX_BYTES,
    MAX_LINE_LENGTH,
)

from .base import (
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    tool_execution_policy,
)
from .broker_tool_input_schema import (
    BrokerToolFieldPresentation,
    broker_tool_input_spec_from_model,
)

READ_TOOL_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="path",
        description=(
            "Local path such as `.`, `README.md`, `src/app.py`, or an "
            "absolute path. Allowed paths follow Read/search access in "
            "Workspace Path Rules."
        ),
    ),
    BrokerToolFieldPresentation(
        name="offset",
        description=(
            "Optional 1-based starting line or directory entry index. "
            "Omit or use 1 for the first page. Use next_offset from a "
            "prior read result when continuing."
        ),
    ),
    BrokerToolFieldPresentation(
        name="column",
        description=(
            "Optional 1-based starting column for a text file. Omit or use 1 "
            "for a new line; when next_column is not null, continue with both "
            "next_offset and next_column."
        ),
    ),
    BrokerToolFieldPresentation(
        name="limit",
        description=(
            "Optional count of lines or directory entries to return "
            "starting at offset. Defaults to 2000."
        ),
    ),
    BrokerToolFieldPresentation(
        name="start_unit",
        description=(
            "Documents only: the 1-based unit the extracted text starts at "
            "(a page, sheet, slide, cell or paragraph, as unit_kind says), so "
            "a long document can be read past the extraction budget. Omit for "
            "the first unit, and continue with next_start_unit. Keep the same "
            "start_unit while paging that text with offset."
        ),
    ),
)

READ_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="read",
        name="Read File",
        description="Read a local file or directory through the broker.",
        guide=ToolGuideSpec(
            what="Use this to inspect readable local files, PDFs, Word documents, Excel workbooks, PowerPoint decks, Jupyter notebooks, image attachments, or immediate directory entries.",
            when="Use when you need current file contents or a quick directory listing before editing or executing commands.",
            pitfalls=(
                "Read one concrete path. Use glob or grep for discovery. "
                "A .pdf, .docx, .xlsx, .pptx or .ipynb path returns "
                "kind=document: "
                "extracted text on the same cursor as a text file, plus an "
                "outline, where embedded images and charts sit, and notes "
                "saying what the extraction left out. A workbook comes back as "
                "one table per sheet, carrying saved values rather than "
                "formulas; a deck as one section per slide, whose text can be "
                "less than the slide shows; a notebook as one section per cell, "
                "carrying the outputs it last saved rather than a fresh run; a "
                "PDF as one section per page, carrying the text it stores "
                "rather than the page as it looks, with the pages that have no "
                "text at all listed in pages_without_text. "
                "Read notes before treating the text as the whole "
                "document. Extraction starts at start_unit and stops at a size "
                "budget, so on truncation_reason=document_budget continue with "
                "start_unit=next_start_unit, and treat next_start_unit=null as "
                "content no further read can reach. "
                "For text files, offset is the 1-based starting line and "
                "column is the 1-based starting character in that line. "
                "limit is the count of lines to return from that start. "
                "Do not guess large offsets: when next_offset is not null, "
                "continue with offset=next_offset and column=next_column; never "
                "request an offset "
                "greater than total_lines when total_lines is known. "
                "If truncated=true, treat the result as incomplete and follow "
                "truncation_reason and retry_hint instead of treating "
                "next_offset=null as EOF. "
                "Each call returns at most limit lines, "
                f"{MAX_BYTES // 1024} KB of text, and what fits the "
                f"{ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT:,}-character inline "
                "result with its metadata; follow the returned cursor for the "
                "rest. Every line of a text file of any size is reachable by "
                f"offset, and a line longer than {MAX_LINE_LENGTH} characters "
                "continues with next_column. A directory read pages through "
                "every entry in the directory's own order, and offset counts the "
                "entries it skips too: symlinks without full read access, "
                "Pantaray's private app storage, and its .runtime-temp folder in "
                "the scratch workspace. warning says what a page skipped."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            required_capabilities=("scoped_read",),
            # Extracting a document costs CPU time that a text read does not.
            default_timeout_ms=30_000,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=ReadToolArgs,
            fields=READ_TOOL_FIELD_PRESENTATION,
            description="Read a local file or directory.",
        ),
        output_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["file", "document", "attachment", "directory"],
                },
                "path": {"type": "string"},
                "content": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "description": "The 1-based starting line or entry index used.",
                },
                "column": {
                    "type": "integer",
                    "description": "The 1-based starting column used for text.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "The last text line returned in this response.",
                },
                "end_column": {
                    "type": "integer",
                    "description": "The last source column returned on end_line.",
                },
                "total_lines": {
                    "type": ["integer", "null"],
                    "description": (
                        "The exact total number of lines in the text file when "
                        "it can be counted safely. Null means the file was too "
                        "large to count in one read call. Continue with next_offset "
                        "when present; otherwise follow retry_hint."
                    ),
                },
                "next_offset": {
                    "type": ["integer", "null"],
                    "description": (
                        "Use this value as offset to continue reading. Null normally "
                        "means there is no next page, unless truncated=true; in that "
                        "case inspect truncation_reason and retry_hint."
                    ),
                },
                "next_column": {
                    "type": ["integer", "null"],
                    "description": (
                        "Use with next_offset to continue within a long line. "
                        "Null means there is no next text page."
                    ),
                },
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["file", "directory"],
                            },
                        },
                        "required": ["name", "kind"],
                        "additionalProperties": False,
                    },
                },
                "truncated": {
                    "type": "boolean",
                    "description": (
                        "True means the tool stopped before proving the whole "
                        "file or directory was exhausted."
                    ),
                },
                "truncation_reason": {
                    "type": ["string", "null"],
                    "enum": [
                        "page_limit",
                        "line_count_budget",
                        "document_budget",
                        None,
                    ],
                    "description": (
                        "page_limit means more entries or lines are available. "
                        "line_count_budget means a text file was "
                        "too large to count total_lines safely. "
                        "document_budget means document extraction stopped before "
                        "the end of the document; notes say what was left out and "
                        "no offset continues it."
                    ),
                },
                "retry_hint": {
                    "type": ["string", "null"],
                    "description": (
                        "Guidance for continuing or retrying when truncated=true."
                    ),
                },
                "warning": {
                    "type": ["string", "null"],
                    "description": "Directory entries this read skipped, and why.",
                },
                "document_format": {
                    "type": "string",
                    "enum": ["docx", "ipynb", "pdf", "pptx", "xlsx"],
                },
                "unit_kind": {
                    "type": "string",
                    "enum": ["cell", "page", "paragraph", "sheet", "slide"],
                    "description": "What total_units counts in this document.",
                },
                "start_unit": {
                    "type": "integer",
                    "description": "The 1-based unit the extracted text starts at.",
                },
                "total_units": {
                    "type": "integer",
                    "description": (
                        "The whole document's unit count, even when this result "
                        "carries only part of the text."
                    ),
                },
                "next_start_unit": {
                    "type": ["integer", "null"],
                    "description": (
                        "The unit extraction stopped at. Read the same path again "
                        "with start_unit set to it to continue. Null means no "
                        "further read reaches more of this document."
                    ),
                },
                "outline": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Headings, or sheet and slide headings, in document order. "
                        "A PDF's bookmarks cover the whole document; every other "
                        "format, and images, charts and pages_without_text, cover "
                        "the units this call extracted."
                    ),
                },
                "images": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                            "ref": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                        "required": ["location", "ref", "count"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Where embedded images sit and how many are at each place. "
                        "This tool returns text only; it does not render them."
                    ),
                },
                "charts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "location": {"type": "string"},
                            "title": {"type": "string"},
                        },
                        "required": ["location", "title"],
                        "additionalProperties": False,
                    },
                },
                "pages_without_text": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Pages that carry no extractable text layer.",
                },
                "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "What this extraction dropped or cut. Read these before "
                        "treating the text as the whole document."
                    ),
                },
                "mime_type": {"type": "string"},
                "message": {"type": "string"},
                "attachments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["file"]},
                            "mime_type": {"type": "string"},
                            "path": {"type": "string"},
                            "ref": {"type": "string"},
                            "byte_size": {"type": "integer"},
                        },
                        "required": [
                            "type",
                            "mime_type",
                            "path",
                            "ref",
                            "byte_size",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["kind", "path"],
            "additionalProperties": False,
        },
    )
)

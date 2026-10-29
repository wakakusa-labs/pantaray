"""Brokered HTML-to-PDF export tool definition."""

from __future__ import annotations

from typing import cast

from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    SAVE_HTML_AS_PDF_TOOL_ID,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    SaveHtmlAsPdfOutput,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ToolConcurrency
from pantaray_agents.tools.files.render_pages import SaveHtmlAsPdfToolArgs

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
from .render_html_page_tool import RENDER_HTML_PAGE_TIMEOUT_MS

SAVE_HTML_AS_PDF_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="path",
        description=(
            "Local path of the .html or .htm file to print, the same path read "
            "takes. Allowed paths follow Read/search access in Workspace Path Rules."
        ),
    ),
    BrokerToolFieldPresentation(
        name="pdf_path",
        description=(
            "Where to save the PDF: a path ending in .pdf, absolute or relative to "
            "the current workspace cwd, in a folder that already exists. An "
            "existing file there is replaced."
        ),
    ),
)

SAVE_HTML_AS_PDF_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=SAVE_HTML_AS_PDF_TOOL_ID,
        name="Save HTML As PDF",
        description=(
            "Print a local HTML file with the app's built-in browser engine and "
            "save the result as a PDF file."
        ),
        guide=ToolGuideSpec(
            what=(
                "Print a local .html or .htm file with the app's built-in browser "
                "engine, using its print CSS and @page rules (A4 when it sets no "
                "page size), and save the PDF at pdf_path. The pages are the ones "
                "render_html_page shows."
            ),
            when=(
                "Use to export a slide deck, a quote or a report written in HTML "
                "to PDF. Use this instead of starting Chrome or another browser "
                "through bash. Check the look with render_html_page first."
            ),
            pitfalls=(
                "Scripts run, but dialogs and window.print() do nothing. Local "
                "files load only from folders read can open; network resources "
                "load only when the command network setting is on. Refused "
                "requests are listed in blocked_requests, and remote iframes are "
                "always refused. Writing pdf_path follows apply_patch's rules: a "
                "path outside registered workspaces waits for the user to approve "
                "that one write, so use one only when the user asked for that "
                "location."
            ),
        ),
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="surgical_edit",
            required_capabilities=("scoped_write",),
            default_timeout_ms=RENDER_HTML_PAGE_TIMEOUT_MS,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=SaveHtmlAsPdfToolArgs,
            fields=SAVE_HTML_AS_PDF_FIELD_PRESENTATION,
            description="Print a local HTML file and save it as a PDF.",
        ),
        output_schema=cast(
            dict[str, JSONValue], SaveHtmlAsPdfOutput.model_json_schema()
        ),
    )
)

__all__ = ["SAVE_HTML_AS_PDF_TOOL"]

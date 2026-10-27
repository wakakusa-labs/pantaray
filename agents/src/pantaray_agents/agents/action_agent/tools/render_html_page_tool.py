"""Brokered HTML page rendering tool definition."""

from __future__ import annotations

from typing import cast

from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    RenderHtmlPageOutput,
)
from pantaray_agents.local_runtime.tooling.documents import MAX_RENDERED_PAGES
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import ToolConcurrency
from pantaray_agents.tools.files.render_pages import RenderHtmlPageToolArgs

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

RENDER_HTML_PAGE_TOOL_ID = "render_html_page"
# Declared like every tool's, but never applied: this tool runs alone in its
# turn, and the desktop app's print and the page renderer each end themselves.
RENDER_HTML_PAGE_TIMEOUT_MS = 60_000

RENDER_HTML_PAGE_FIELD_PRESENTATION = (
    BrokerToolFieldPresentation(
        name="path",
        description=(
            "Local path of an .html or .htm file, the same path read takes. "
            "Allowed paths follow Read/search access in Workspace Path Rules."
        ),
    ),
    BrokerToolFieldPresentation(
        name="pages",
        description=(
            "Optional. 1-based page numbers of the printed layout, listed one by "
            "one: pages 1 to 3 is [1, 2, 3]. At most "
            f"{MAX_RENDERED_PAGES}, with no repeats. Omit it to look at pages 1 "
            f"to {MAX_RENDERED_PAGES}."
        ),
    ),
)

RENDER_HTML_PAGE_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=RENDER_HTML_PAGE_TOOL_ID,
        name="Look At HTML Pages",
        description=(
            "Print a local HTML file with the app's built-in browser engine and "
            "look at its pages as images this step can see."
        ),
        guide=ToolGuideSpec(
            what=(
                "Lay out a local .html or .htm file for print with the app's "
                "built-in browser engine, using its print CSS and @page rules "
                "(A4 when it sets no page size), and attach the requested pages "
                "to this step as images."
            ),
            when=(
                "Use to check the look of HTML you wrote or edited, such as a "
                "slide deck, a quote or a report. Use this instead of starting "
                "Chrome or another browser through bash."
            ),
            pitfalls=(
                "Scripts run, but dialogs and window.print() do nothing. Local "
                "files load only from folders read can open; network resources "
                "load only when the command network setting is on. Refused "
                "requests are listed in blocked_requests, and remote iframes are "
                "always refused. A design made only for the screen may look "
                "different in print. At most "
                f"{MAX_RENDERED_PAGES} pages per call; page_count gives the total "
                "and pages past the end are left out. The images stay with this "
                "step until the context window is rebuilt, and are not sent "
                "again after that, so look again if you no longer see them. A "
                "subagent cannot see them."
            ),
        ),
        # Sequential although it only reads, for the reason render_pdf_page is:
        # one call returns up to 8 images.
        concurrency=ToolConcurrency("sequential"),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            required_capabilities=("scoped_read",),
            default_timeout_ms=RENDER_HTML_PAGE_TIMEOUT_MS,
        ),
        input_spec=broker_tool_input_spec_from_model(
            model=RenderHtmlPageToolArgs,
            fields=RENDER_HTML_PAGE_FIELD_PRESENTATION,
            description="Print a local HTML file and look at its pages as images.",
        ),
        output_schema=cast(
            dict[str, JSONValue], RenderHtmlPageOutput.model_json_schema()
        ),
    )
)

__all__ = [
    "RENDER_HTML_PAGE_TIMEOUT_MS",
    "RENDER_HTML_PAGE_TOOL",
    "RENDER_HTML_PAGE_TOOL_ID",
]

"""Raw computer-activity read tools for the user-initiated Action conversation.

The short Insight agent owns the same two reads (``insight_agent.zanei_tools``).
This module only declares the Action-facing contract; the runtime handler reuses
that implementation so both agents share one reader, cursor and evidence path.
"""

from __future__ import annotations

from typing import get_args

from pantaray_agents.local_runtime.context.source_protocol import (
    SQLITE_INTEGER_MAX,
    EvidenceField,
)
from pantaray_agents.schema.agent.base import JSONValue

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)

ZANEI_TIMELINE_TOOL_ID = "zanei_timeline"
ZANEI_QUERY_TOOL_ID = "zanei_query"
ZANEI_EVIDENCE_FIELDS: tuple[str, ...] = get_args(EvidenceField.__value__)
RECORDING_UNAVAILABLE_STATUS = "recording_unavailable"
# One local subprocess read has a 10 s deadline; leave room for process startup.
ZANEI_READ_TIMEOUT_MS = 30_000

_EVENT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "event_id": {"type": "string"},
        "observed_at": {"type": "string"},
        "event_type": {"type": ["string", "null"]},
        "source": {"type": ["string", "null"]},
        "app": {"type": ["string", "null"]},
        "bundle_id": {"type": ["string", "null"]},
        "window_title": {"type": ["string", "null"]},
        "window_id": {"type": ["integer", "null"]},
    },
    "required": [
        "event_id",
        "observed_at",
        "event_type",
        "source",
        "app",
        "bundle_id",
        "window_title",
        "window_id",
    ],
}

_UNAVAILABLE_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": [RECORDING_UNAVAILABLE_STATUS]},
        "message": {"type": "string"},
    },
    "required": ["status", "message"],
}

# A run may read at most MAX_TIMELINE_PAGES_PER_RUN pages, and fewer when they
# are large; the reader then reports the spent budget beside an empty page.
_PAGE_BUDGET_PROPERTIES: dict[str, JSONValue] = {
    "page_budget_spent": {"type": "boolean"},
    "note": {"type": "string"},
}

_TIMELINE_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "timezone": {"type": ["string", "null"]},
                "events": {"type": "array", "items": _EVENT_SCHEMA},
                "has_more": {"type": "boolean"},
                # Set when the page was cut to the newest events that fit one
                # tool result; see ACTION_TIMELINE_RESULT_CHARACTER_BUDGET.
                "older_events_omitted": {"type": "integer", "minimum": 1},
                **_PAGE_BUDGET_PROPERTIES,
            },
            "required": ["events", "has_more"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "gap": {"type": "string"},
                "has_more": {"type": "boolean"},
                **_PAGE_BUDGET_PROPERTIES,
            },
            "required": ["gap", "has_more"],
        },
        _UNAVAILABLE_SCHEMA,
    ],
}

_QUERY_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "next_start": {"type": ["integer", "null"]},
                "redacted": {"type": "boolean"},
                "source_truncated": {"type": "boolean"},
            },
            "required": ["text", "next_start", "redacted", "source_truncated"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["absent", "expired", "denied"]},
            },
            "required": ["status"],
        },
        _UNAVAILABLE_SCHEMA,
    ],
}


ZANEI_TIMELINE_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=ZANEI_TIMELINE_TOOL_ID,
        name="Recent Computer Activity",
        description=(
            "Read the user's own recent computer activity that has not been "
            "summarized yet: which app, window and browser tab was in use, and when."
        ),
        guide=ToolGuideSpec(
            what=(
                "Reads one page of the local computer-activity recording, oldest "
                "first, starting where the last activity summary stopped. Each event "
                "carries its time, event type, app name, bundle id and window title. "
                "Captured bodies such as page text or copied text are not included "
                "here; read them per event with zanei_query. A page too large for "
                "one result keeps its newest events and reports the rest as "
                "older_events_omitted."
            ),
            when=(
                "Use when the user points at something outside this conversation - "
                "'this article', 'the page I was just looking at', 'the error I just "
                "saw', 'what was I doing'. Call it again while has_more is true to "
                "reach the newest events, then pick the event that matches and read "
                "one field with zanei_query. To answer about a web page, take its "
                "url with zanei_query and fetch the page with web_extract."
            ),
            pitfalls=(
                "Recording can be off or stopped; the result is then "
                f"status='{RECORDING_UNAVAILABLE_STATUS}' and you must ask the user "
                "for the page or text instead of guessing. A result with "
                "page_budget_spent means no further page can be read in this "
                "conversation; answer from the events you already have. Event ids "
                "are valid only for events returned during this run. This is not "
                "general recall of stored knowledge - use memory_search for that."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=ZANEI_READ_TIMEOUT_MS,
        ),
        input_spec=InputSpec(
            description="Read the next page of recent computer activity.",
        ),
        output_schema=_TIMELINE_OUTPUT_SCHEMA,
    )
)


ZANEI_QUERY_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id=ZANEI_QUERY_TOOL_ID,
        name="Computer Activity Detail",
        description=(
            "Read one recorded field of one event returned by zanei_timeline, such "
            "as the browser url or the captured text of that moment."
        ),
        guide=ToolGuideSpec(
            what=(
                "Returns one field of one recorded event: url for the browser "
                "location, tab_title and window_title for what was open, text for "
                "captured or copied text, element_value for an accessibility value. "
                "Long text is returned in slices with a next_start byte offset."
            ),
            when=(
                "Use right after zanei_timeline, on the event that matches what the "
                "user is referring to. Continue truncated text by passing the "
                "next_start value from the previous result as start."
            ),
            pitfalls=(
                "event_id must come from this run's zanei_timeline result. A field "
                "can come back as absent, expired or denied; that means there is no "
                "data to read, not a failure to retry. For a web page prefer url "
                "plus web_extract over reading a long captured text body."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="read_only",
            default_timeout_ms=ZANEI_READ_TIMEOUT_MS,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="event_id",
                    schema={"type": "string", "minLength": 1},
                    required=True,
                    description="event_id from this run's zanei_timeline result.",
                ),
                field_spec(
                    name="field",
                    schema={"type": "string", "enum": list(ZANEI_EVIDENCE_FIELDS)},
                    required=True,
                    description="Recorded field to read from that event.",
                ),
                field_spec(
                    name="start",
                    # The read protocol caps the offset at SQLite's max integer;
                    # declare it so an over-range value is repaired as a
                    # malformed argument instead of failing inside the request.
                    schema={
                        "type": "integer",
                        "minimum": 0,
                        "maximum": SQLITE_INTEGER_MAX,
                    },
                    description=(
                        "UTF-8 byte offset to start from. Default 0. Use next_start "
                        "from a previous result to continue truncated text."
                    ),
                ),
            )
        ),
        output_schema=_QUERY_OUTPUT_SCHEMA,
    )
)


__all__ = [
    "RECORDING_UNAVAILABLE_STATUS",
    "ZANEI_EVIDENCE_FIELDS",
    "ZANEI_QUERY_TOOL",
    "ZANEI_QUERY_TOOL_ID",
    "ZANEI_TIMELINE_TOOL",
    "ZANEI_TIMELINE_TOOL_ID",
]

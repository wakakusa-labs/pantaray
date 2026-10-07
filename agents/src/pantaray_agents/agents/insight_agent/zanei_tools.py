"""Read the current source incrementally; only this agent receives raw tools."""

import json
from dataclasses import dataclass, field
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.local_runtime.context.source_gate import ActiveSource
from pantaray_agents.local_runtime.context.source_protocol import (
    DeniedResponse,
    EvidenceField,
    EvidenceOrigin,
    EvidenceReadRequest,
    EvidenceResponse,
    ExpiredResponse,
    GapResponse,
    Observation,
    ObservationText,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.context.source_reader import ReadResult, SourceReader
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_agents.utils.local_time import describe_utc_timestamp, local_zone_name

from .record_verification import ReadEvent

PAGE_TOOL = "zanei_timeline"
EVENT_TOOL = "zanei_query"
# Keep each source read within the protocol page limit.
PAGE_LIMIT = 128
# One zanei_query result per call. `next_start` pages through longer text, so
# this bounds a call's yield, not what a run can read.
EVENT_TEXT_CHARACTERS = 2000
# Projection caps, per kind of field. `window_title` and the app display name
# are free text an application or a web page chooses, so they need room to stay
# recognizable; `event_type` and `source` are recorder enums and `bundle_id` is
# a macOS reverse-DNS identifier, none of which need more.
FREE_TEXT_CHARACTERS = 100
IDENTIFIER_CHARACTERS = 48
# Timeline results remain in the run transcript. Repeated metadata is shared per
# page so a 15-minute window (~1,125 events at the observed ~75/minute) fits
# without increasing the character budget. Twelve pages leave room for detailed
# evidence reads within the existing 36-call run budget.
# Design limit: the character budget may be exceeded by one source page because
# cursors are opaque. If normal 15-minute windows stop at this bound, measure
# projection size and evidence coverage before changing either budget.
MAX_TIMELINE_PAGES_PER_RUN = 12
MAX_TIMELINE_CHARS_PER_RUN = 110_000


class _PageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _EventArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str
    field: EvidenceField
    start: Annotated[int, Field(ge=0)] = 0


# Shared with the Suggestion run, which offers the same two reads.
PAGE_REQUEST_SCHEMA = _PageArguments.model_json_schema()
EVENT_REQUEST_SCHEMA = _EventArguments.model_json_schema()


@dataclass(slots=True)
class ZaneiTools:
    reader: SourceReader
    source: ActiveSource
    cursor: str | None
    upper_bound: str | None
    first_page: PageResponse | GapResponse | None = None
    observations: dict[str, Observation] = field(default_factory=dict)
    # Every chunk zanei_query handed the model, keyed by field and byte offset.
    # It is what the claimed records are verified against, so it lives as long
    # as the run and reaches neither the log nor the store.
    fetched: dict[tuple[str, EvidenceField], dict[int, str]] = field(
        default_factory=dict
    )
    read_started: bool = False
    has_more: bool = True
    events_read: int = 0
    last_append_sequence: int | None = None
    pages_read: int = 0
    timeline_chars: int = 0
    # Resolved once so every page of a run names the same zone as its offsets.
    zone_name: str | None = field(default_factory=local_zone_name)

    @property
    def page_budget_spent(self) -> bool:
        return (
            self.pages_read >= MAX_TIMELINE_PAGES_PER_RUN
            or self.timeline_chars >= MAX_TIMELINE_CHARS_PER_RUN
        )

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        # Results are the existing context-read protocol, projected for prompts.
        response_schema: dict[str, JSONValue] = {"type": "object"}
        return (
            ReactToolDefinition(
                name=PAGE_TOOL,
                description=(
                    "Read the next page of this run's unprocessed Zanei range. "
                    "Each events row follows event_columns; context_index selects the "
                    "page-local contexts entry. Every event ID and timestamp is retained. The host "
                    "keeps the cursor; call again to continue. Query only useful "
                    "events with zanei_query. Read the whole range: call this until "
                    "has_more is false, then complete. A run may read at most "
                    f"{MAX_TIMELINE_PAGES_PER_RUN} pages, and fewer when the "
                    "pages are large; once a result reports page_budget_spent, "
                    "complete with what you read."
                ),
                request_schema=PAGE_REQUEST_SCHEMA,
                response_schema=response_schema,
                execute=self.timeline,
            ),
            ReactToolDefinition(
                name=EVENT_TOOL,
                description=(
                    "Read one field of an event returned by zanei_timeline. "
                    "Use text for captured/copied text, element_value for AX "
                    "values, url for browser location. start is a UTF-8 byte "
                    "offset; use next_start to continue truncated text."
                ),
                request_schema=EVENT_REQUEST_SCHEMA,
                response_schema=response_schema,
                execute=self.query,
            ),
        )

    async def timeline(self, call: ReactToolCall, _step: int) -> ReactToolResult:
        _PageArguments.model_validate(call.tool_args)
        if not self.has_more or self.page_budget_spent:
            return _success(
                call, self._page_output({"events": [], "has_more": self.has_more})
            )
        result: ReadResult | None = self.first_page
        self.first_page = None
        if result is None:
            result = await self.reader.read_page(
                self.source,
                PageReadRequest(
                    cursor=self.cursor, upper_bound=self.upper_bound, limit=PAGE_LIMIT
                ),
            )
        self.pages_read += 1
        if isinstance(result, GapResponse):
            self.read_started = True
            self.cursor = result.resume_cursor
            self.upper_bound = result.upper_bound
            # The gap consumed the range through `through`; the warning must report
            # where the run actually stopped, not the last sequence it read.
            self.last_append_sequence = result.affected_range.through
            return _success(
                call, self._page_output({"gap": result.reason, "has_more": True})
            )
        if not isinstance(result, PageResponse):
            raise RuntimeError(f"Zanei page read failed: {type(result).__name__}")
        self.read_started = True
        self.cursor = result.next_cursor
        self.upper_bound = result.upper_bound
        self.has_more = result.has_more
        self.events_read += len(result.observations)
        if result.observations:
            self.last_append_sequence = result.observations[-1].append_sequence
        self.observations.update((item.id, item) for item in result.observations)
        return _success(
            call,
            self._page_output(
                {
                    **_project_events(result.observations, self.zone_name),
                    "has_more": self.has_more,
                }
            ),
        )

    def _page_output(self, output: dict[str, JSONValue]) -> dict[str, JSONValue]:
        """Charge this result to the run's budgets and say when they are spent.

        Every timeline result is charged, including the empty ones returned once
        a budget is spent, because each one enters the transcript. The charge is
        applied before the budget is read, so the page that crosses a budget is
        the page that reports it.
        """
        self.timeline_chars += len(json.dumps(output, ensure_ascii=False))
        if output["has_more"] and self.page_budget_spent:
            output["page_budget_spent"] = True
            output["note"] = (
                "This run's page budget is spent. Complete using the events already "
                "read; the next run resumes from where this one stopped."
            )
        return output

    async def query(self, call: ReactToolCall, _step: int) -> ReactToolResult:
        args = _EventArguments.model_validate(call.tool_args)
        observation = self.observations.get(args.event_id)
        if observation is None:
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="ZANEI_EVENT_OUT_OF_RANGE",
                message="Select an event returned by this run's zanei_timeline.",
            )
        result = await self.reader.read_evidence(
            self.source,
            EvidenceReadRequest(
                origin=EvidenceOrigin(
                    store_identity=self.source.binding.store_id,
                    append_sequence=observation.append_sequence,
                    event_id=observation.id,
                    observed_at=observation.ts,
                    field=args.field,
                ),
                start=args.start,
                end=None,
            ),
        )
        if isinstance(result, (ExpiredResponse, DeniedResponse)):
            return _success(call, {"status": result.kind})
        if not isinstance(result, EvidenceResponse):
            # A single unreadable field must not end a run that already read events.
            return tool_error_response(
                tool_name=call.tool_name,
                error_code="ZANEI_EVENT_UNREADABLE",
                message=f"This field could not be read ({type(result).__name__}).",
            )
        if result.content.kind == "absent":
            return _success(call, {"status": "absent"})
        text = result.content.text[:EVENT_TEXT_CHARACTERS]
        self.fetched.setdefault((args.event_id, args.field), {})[args.start] = text
        next_start = args.start + len(text.encode("utf-8"))
        return _success(
            call,
            {
                "text": text,
                "next_start": next_start
                if next_start < result.content.total_bytes
                else None,
                "redacted": result.metadata.redaction_applied,
                "source_truncated": result.metadata.truncated,
            },
        )

    def read_events(self) -> dict[str, ReadEvent]:
        """The text this run read, per event, with that event's metadata.

        Join overlapping or adjacent UTF-8 ranges only. Unread gaps and separate
        fields must never create adjacencies that did not exist on screen.
        Metadata uses the observation itself, not the capped prompt projection.
        """
        texts: dict[str, list[str]] = {}
        for (event_id, _evidence_field), chunks in self.fetched.items():
            spans: list[bytes] = []
            end = -1
            for start, chunk in sorted(chunks.items()):
                encoded = chunk.encode("utf-8")
                if start > end:
                    spans.append(encoded)
                else:
                    spans[-1] += encoded[end - start :]
                end = max(end, start + len(encoded))
            texts.setdefault(event_id, []).extend(
                span.decode("utf-8") for span in spans
            )
        return {
            event_id: ReadEvent(
                observed_at=self.observations[event_id].ts,
                app_name=_recorded(self.observations[event_id].app_name),
                bundle_id=_recorded(self.observations[event_id].bundle_id),
                window_title=_recorded(self.observations[event_id].window_title),
                texts=tuple(field_texts),
            )
            for event_id, field_texts in texts.items()
        }


def _recorded(value: ObservationText) -> str | None:
    """The recorded value, or None when the page carried no text for it."""
    return value.text if value.kind == "value" else None


def _project_events(
    observations: tuple[Observation, ...], zone_name: str | None
) -> dict[str, JSONValue]:
    """Keep every event and timestamp; share only identical display metadata.

    Timestamps are the user's local time; the zone is named once per page.
    """
    fields = ("event_type", "source", "app", "bundle_id", "window_title", "window_id")
    context_ids: dict[tuple[str | int | None, ...], int] = {}
    contexts: list[JSONValue] = []
    events: list[JSONValue] = []
    for item in observations:
        context = (
            _text(item.event_type, IDENTIFIER_CHARACTERS),
            _text(item.source, IDENTIFIER_CHARACTERS),
            _text(item.app_name, FREE_TEXT_CHARACTERS),
            _text(item.bundle_id, IDENTIFIER_CHARACTERS),
            _text(item.window_title, FREE_TEXT_CHARACTERS),
            item.window_id,
        )
        if context not in context_ids:
            context_ids[context] = len(contexts)
            contexts.append(dict(zip(fields, context, strict=True)))
        events.append([item.id, describe_utc_timestamp(item.ts), context_ids[context]])
    return {
        "timezone": zone_name,
        "event_columns": ["event_id", "observed_at", "context_index"],
        "contexts": contexts,
        "events": events,
    }


def _text(value: ObservationText, cap: int) -> str | None:
    if value.kind == "absent":
        return None
    if value.kind == "omitted":
        return f"[omitted: {value.utf8_bytes} bytes; use zanei_query]"
    return value.text if len(value.text) <= cap else value.text[:cap] + " [truncated]"


def _success(call: ReactToolCall, output: dict[str, JSONValue]) -> ReactToolResult:
    return ReactToolResult(tool_name=call.tool_name, status="success", output=output)

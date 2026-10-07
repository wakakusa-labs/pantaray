"""Computer activity recorded after the Insight a Suggestion run decides on.

The reads belong to ``tools.zanei.ZaneiTools``. This session opens
one per run at the cursor the triggering short Insight committed, so the run sees
what happened after the observations in its prompt even when later Insights have
moved the shared cursor. It never writes a cursor. The Suggestion job owns the
read permit: it runs the whole agent under ``SourceGate.track`` and publishes
under ``SourceGate.guard``, so revocation is handled there, not per read.
"""

from __future__ import annotations

from dataclasses import dataclass

from pantaray_agents.local_runtime.context.source_gate import (
    ActiveSource,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.context.source_reader import SourceReader
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    tool_error_response,
)
from pantaray_agents.tools.zanei import (
    EVENT_REQUEST_SCHEMA,
    EVENT_TOOL,
    MAX_TIMELINE_PAGES_PER_RUN,
    PAGE_REQUEST_SCHEMA,
    PAGE_TOOL,
    ZaneiTools,
)

RECORDING_UNAVAILABLE_STATUS = "recording_unavailable"
ZANEI_READ_FAILED_ERROR_CODE = "ZANEI_READ_FAILED"
LATEST_NOT_REACHED_NOTE = (
    "The page budget ran out before the newest activity. The unread activity is "
    "unknown: it does not show that anything is unchanged or resolved."
)


@dataclass(frozen=True, slots=True)
class InsightActivityStart:
    """The live read permit and the cursor the triggering Insight committed."""

    source: ActiveSource
    cursor: str


@dataclass(slots=True)
class SuggestionZaneiSession:
    reader: SourceReader
    # None when recording is not readable or the Insight predates stored cursors.
    start: InsightActivityStart | None
    tools: ZaneiTools | None = None

    def definitions(self) -> tuple[ReactToolDefinition, ...]:
        return (
            ReactToolDefinition(
                name=PAGE_TOOL,
                description=(
                    "Read the computer activity recorded after the latest "
                    "short-term Insight's observations, oldest first, up to the "
                    "first call. Each events row follows event_columns; "
                    "context_index selects the page-local contexts entry. The "
                    "host keeps the cursor; call again while has_more is true, "
                    "because the newest events come last. No events with "
                    "has_more false means nothing was recorded since. A run may "
                    f"read at most {MAX_TIMELINE_PAGES_PER_RUN} pages, and fewer "
                    "when they are large; reached_latest false means the newest "
                    "activity was left unread."
                ),
                request_schema=PAGE_REQUEST_SCHEMA,
                response_schema={"type": "object"},
                execute=self._timeline,
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
                response_schema={"type": "object"},
                execute=self._query,
            ),
        )

    async def _timeline(self, call: ReactToolCall, step: int) -> ReactToolResult:
        result = await self._read(call, step, timeline=True)
        output = result.output
        if isinstance(output, dict) and output.get("page_budget_spent"):
            # Replace the short Insight's "the next run resumes" note: no later
            # Suggestion run continues from here.
            output["reached_latest"] = False
            output["note"] = LATEST_NOT_REACHED_NOTE
        return result

    async def _query(self, call: ReactToolCall, step: int) -> ReactToolResult:
        return await self._read(call, step, timeline=False)

    async def _read(
        self, call: ReactToolCall, step: int, *, timeline: bool
    ) -> ReactToolResult:
        if self.start is None:
            return ReactToolResult(
                tool_name=call.tool_name,
                status="success",
                output={
                    "status": RECORDING_UNAVAILABLE_STATUS,
                    "message": "Activity after this Insight cannot be read in this run.",
                },
            )
        # One session per run, so the page budget and cursor span every call.
        if self.tools is None:
            self.tools = ZaneiTools(
                reader=self.reader,
                source=self.start.source,
                cursor=self.start.cursor,
                upper_bound=None,
            )
        try:
            if timeline:
                return await self.tools.timeline(call, step)
            return await self.tools.query(call, step)
        except SourceInvalidated:
            # A revoked permit belongs to the job that holds it.
            raise
        except RuntimeError as exc:
            # ZaneiTools raises this for a reader transport or protocol failure.
            # The run decides without this evidence instead of failing.
            return tool_error_response(
                tool_name=call.tool_name,
                error_code=ZANEI_READ_FAILED_ERROR_CODE,
                message=str(exc),
            )


__all__ = [
    "InsightActivityStart",
    "RECORDING_UNAVAILABLE_STATUS",
    "SuggestionZaneiSession",
]

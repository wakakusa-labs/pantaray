from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from pantaray_agents.local_runtime.context.source_gate import (
    ActiveSource,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.context.source_protocol import (
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.context.source_reader import ReadTimeout
from pantaray_agents.local_runtime.tooling.suggestion_research.zanei import (
    RECORDING_UNAVAILABLE_STATUS,
    InsightActivityStart,
    SuggestionZaneiSession,
)
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope
from pantaray_agents.tools.zanei import (
    MAX_TIMELINE_PAGES_PER_RUN,
    PAGE_TOOL,
)

STORE_ID = "store-1"


def _start(cursor: str) -> InsightActivityStart:
    binding = SourceBinding(
        user_id="user-1",
        epoch=UUID(int=1),
        policy_revision="policy-1",
        store_id=STORE_ID,
        protocol_version=1,
    )
    return InsightActivityStart(source=ActiveSource(binding), cursor=cursor)


def _page(sequence: int, *, has_more: bool) -> PageResponse:
    absent = {"kind": "absent"}
    return PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": (
                {
                    "append_sequence": sequence,
                    "id": f"event-{sequence}",
                    "ts": "2026-09-27T17:56:50Z",
                    "source": absent,
                    "event_type": {"kind": "value", "text": "app.terminate"},
                    "bundle_id": {"kind": "value", "text": "com.github.Electron"},
                    "app_name": {"kind": "value", "text": "Electron"},
                    "pid": None,
                    "window_title": absent,
                    "window_id": None,
                },
            ),
            "next_cursor": f"cursor-{sequence}",
            "upper_bound": "upper-1",
            "has_more": has_more,
            "coverage": {"after": sequence - 1, "through": sequence},
        }
    )


@dataclass
class _FakeReader:
    pages: list[object] = field(default_factory=list)
    requests: list[PageReadRequest] = field(default_factory=list)

    async def read_page(self, _source: ActiveSource, request: PageReadRequest):
        self.requests.append(request)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _session(
    reader: _FakeReader, start: InsightActivityStart | None
) -> SuggestionZaneiSession:
    return SuggestionZaneiSession(reader=reader, start=start)  # type: ignore[arg-type]


async def _timeline(session: SuggestionZaneiSession):
    (definition,) = (d for d in session.definitions() if d.name == PAGE_TOOL)
    call = ReactToolCall(
        tool_name=PAGE_TOOL,
        tool_args={},
        tool_call_envelope=ToolCallEnvelope(tool_id=PAGE_TOOL, reason=None, args={}),
    )
    return await definition.execute(call, 1)


async def test_the_run_reads_on_from_the_cursor_its_insight_committed() -> None:
    reader = _FakeReader(pages=[_page(4, has_more=True), _page(5, has_more=False)])
    session = _session(reader, _start("insight-cursor-3"))

    first = await _timeline(session)
    second = await _timeline(session)

    assert first.status == second.status == "success"
    assert [(r.cursor, r.upper_bound) for r in reader.requests] == [
        ("insight-cursor-3", None),
        ("cursor-4", "upper-1"),
    ]
    assert isinstance(second.output, dict)
    assert "reached_latest" not in second.output


async def test_a_spent_page_budget_says_the_newest_activity_was_not_read() -> None:
    reader = _FakeReader(
        pages=[
            _page(sequence, has_more=True)
            for sequence in range(1, MAX_TIMELINE_PAGES_PER_RUN + 1)
        ]
    )
    session = _session(reader, _start("cursor-0"))

    results = [await _timeline(session) for _ in range(MAX_TIMELINE_PAGES_PER_RUN + 1)]

    assert len(reader.requests) == MAX_TIMELINE_PAGES_PER_RUN
    last = results[-1].output
    assert isinstance(last, dict)
    assert last["reached_latest"] is False
    assert "next run" not in last["note"]


async def test_without_a_start_nothing_is_read() -> None:
    reader = _FakeReader()

    result = await _timeline(_session(reader, None))

    assert isinstance(result.output, dict)
    assert result.output["status"] == RECORDING_UNAVAILABLE_STATUS
    assert reader.requests == []


async def test_a_reader_failure_is_a_tool_error_the_run_can_continue_past() -> None:
    result = await _timeline(_session(_FakeReader(pages=[ReadTimeout()]), _start("c")))

    assert result.status == "error"


async def test_a_revoked_permit_propagates_to_the_job_that_owns_it() -> None:
    reader = _FakeReader(pages=[SourceInvalidated("revoked")])

    with pytest.raises(SourceInvalidated):
        await _timeline(_session(reader, _start("c")))

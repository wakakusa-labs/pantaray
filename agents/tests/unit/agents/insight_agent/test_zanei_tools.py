from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from pantaray_agents.local_runtime.context.source_gate import ActiveSource
from pantaray_agents.local_runtime.context.source_protocol import (
    DeniedResponse,
    EvidenceReadRequest,
    EvidenceResponse,
    ExpiredResponse,
    GapResponse,
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.context.source_reader import ReadTimeout
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope
from pantaray_agents.tools.zanei import (
    EVENT_TEXT_CHARACTERS,
    EVENT_TOOL,
    FREE_TEXT_CHARACTERS,
    IDENTIFIER_CHARACTERS,
    MAX_TIMELINE_CHARS_PER_RUN,
    PAGE_LIMIT,
    PAGE_TOOL,
    ZaneiTools,
)
from pantaray_agents.utils.local_time import describe_utc_timestamp

STORE_ID = "store-1"
# "あ" is three UTF-8 bytes, so character slicing and byte offsets differ. One
# cap's worth plus a tail, so the first read truncates and the second finishes.
MULTIBYTE_TEXT = "あ" * (EVENT_TEXT_CHARACTERS + 1_000)


def _source() -> ActiveSource:
    return ActiveSource(
        binding=SourceBinding(
            user_id="user-1",
            epoch=UUID(int=1),
            policy_revision="policy-1",
            store_id=STORE_ID,
            protocol_version=1,
        )
    )


def _observation(sequence: int) -> dict[str, object]:
    absent = {"kind": "absent"}
    return {
        "append_sequence": sequence,
        "id": f"event-{sequence}",
        "ts": "2026-09-26T21:50:00Z",
        "source": absent,
        "event_type": {"kind": "value", "text": "text_capture"},
        "bundle_id": absent,
        "app_name": {"kind": "value", "text": "Editor"},
        "pid": None,
        "window_title": {"kind": "omitted", "utf8_bytes": 4096},
        "window_id": None,
    }


def _page(
    *, sequences: tuple[int, ...], next_cursor: str, has_more: bool = False
) -> PageResponse:
    return PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": tuple(_observation(sequence) for sequence in sequences),
            "next_cursor": next_cursor,
            "upper_bound": "upper-1",
            "has_more": has_more,
            "coverage": {
                "after": (sequences[0] - 1) if sequences else 0,
                "through": sequences[-1] if sequences else 0,
            },
        }
    )


def _gap() -> GapResponse:
    return GapResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "gap",
            "store_identity": STORE_ID,
            "reason": "retention_or_deletion",
            "affected_range": {"after": 0, "through": 5},
            "resume_cursor": "cursor-after-gap",
            "upper_bound": "upper-2",
        }
    )


def _evidence(*, start: int, end: int, total: int, text: str) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "kind": "evidence",
        "origin": None,
        "content": {
            "kind": "text",
            "text": text,
            "start": start,
            "end": end,
            "total_bytes": total,
            "remaining": None if end == total else (end, total),
        },
        "metadata": {
            "event_type": "text_capture",
            "payload_without_text": {},
            "redaction_applied": False,
            "truncated": False,
        },
    }


@dataclass
class _FakeReader:
    pages: list[object] = field(default_factory=list)
    evidence: list[object] = field(default_factory=list)
    page_requests: list[PageReadRequest] = field(default_factory=list)
    evidence_requests: list[EvidenceReadRequest] = field(default_factory=list)

    async def read_page(
        self, _source: ActiveSource, request: PageReadRequest
    ) -> object:
        self.page_requests.append(request)
        return self.pages.pop(0)

    async def read_evidence(
        self, _source: ActiveSource, request: EvidenceReadRequest
    ) -> object:
        self.evidence_requests.append(request)
        result = self.evidence.pop(0)
        if isinstance(result, dict):
            return EvidenceResponse.model_validate({**result, "origin": request.origin})
        return result


def _call(tool_name: str, **args: object) -> ReactToolCall:
    return ReactToolCall(
        tool_name=tool_name,
        tool_args=args,
        tool_call_envelope=ToolCallEnvelope(tool_id=tool_name, reason=None, args=args),
    )


def _tools(reader: _FakeReader, *, first_page: object = None) -> ZaneiTools:
    return ZaneiTools(
        reader=reader,  # type: ignore[arg-type]
        source=_source(),
        cursor=None,
        upper_bound=None,
        first_page=first_page,  # type: ignore[arg-type]
    )


@pytest.mark.usefixtures("tokyo_local_zone")
async def test_the_first_page_is_reused_before_the_reader_is_called() -> None:
    reader = _FakeReader(pages=[_page(sequences=(2,), next_cursor="cursor-2")])
    tools = _tools(
        reader,
        first_page=_page(sequences=(1,), next_cursor="cursor-1", has_more=True),
    )

    first = await tools.timeline(_call(PAGE_TOOL), 1)
    assert reader.page_requests == []
    assert tools.cursor == "cursor-1"

    await tools.timeline(_call(PAGE_TOOL), 2)
    assert [request.cursor for request in reader.page_requests] == ["cursor-1"]
    assert tools.cursor == "cursor-2"

    # The model reads local time, and the page names the zone once.
    assert first.output["timezone"] == "Asia/Tokyo"  # type: ignore[index,call-overload]
    events = first.output["events"]  # type: ignore[index,call-overload]
    assert events[0] == ["event-1", "2026-09-27T06:50+09:00", 0]
    context = first.output["contexts"][events[0][2]]  # type: ignore[index,call-overload]
    assert context["app"] == "Editor"
    assert "use zanei_query" in str(context["window_title"])


async def test_a_gap_moves_the_read_position_and_reports_missing_evidence() -> None:
    reader = _FakeReader(pages=[_gap()])
    tools = _tools(reader)

    result = await tools.timeline(_call(PAGE_TOOL), 1)

    assert result.output == {"gap": "retention_or_deletion", "has_more": True}
    assert tools.cursor == "cursor-after-gap"
    assert tools.upper_bound == "upper-2"
    assert tools.read_started is True
    # The gap covered sequences 1-5, so that is where this run stopped; reporting
    # the last sequence it read would understate the shortfall.
    assert tools.last_append_sequence == 5


async def test_an_event_outside_this_run_is_rejected() -> None:
    tools = _tools(_FakeReader())

    result = await tools.query(_call(EVENT_TOOL, event_id="event-9", field="text"), 1)

    assert result.status == "error"
    assert "ZANEI_EVENT_OUT_OF_RANGE" in str(result.output)


async def test_truncated_text_returns_a_utf8_byte_offset_to_continue_from() -> None:
    total = len(MULTIBYTE_TEXT.encode("utf-8"))
    resume = EVENT_TEXT_CHARACTERS * 3
    reader = _FakeReader(
        evidence=[
            _evidence(start=0, end=total, total=total, text=MULTIBYTE_TEXT),
            _evidence(
                start=resume,
                end=total,
                total=total,
                text=MULTIBYTE_TEXT[EVENT_TEXT_CHARACTERS:],
            ),
        ]
    )
    tools = _tools(reader, first_page=_page(sequences=(1,), next_cursor="cursor-1"))
    await tools.timeline(_call(PAGE_TOOL), 1)

    first = await tools.query(_call(EVENT_TOOL, event_id="event-1", field="text"), 2)
    assert first.output["next_start"] == resume  # type: ignore[index,call-overload]
    assert len(str(first.output["text"])) == EVENT_TEXT_CHARACTERS  # type: ignore[index,call-overload]

    second = await tools.query(
        _call(EVENT_TOOL, event_id="event-1", field="text", start=resume), 3
    )
    assert second.output["next_start"] is None  # type: ignore[index,call-overload]
    assert reader.evidence_requests[1].start == resume
    assert reader.evidence_requests[1].end is None


async def test_absent_expired_and_denied_reads_are_reported_as_status() -> None:
    reader = _FakeReader(
        evidence=[
            {
                "protocol_version": 1,
                "kind": "evidence",
                "origin": None,
                "content": {"kind": "absent"},
                "metadata": {
                    "event_type": "text_capture",
                    "payload_without_text": {},
                    "redaction_applied": False,
                    "truncated": False,
                },
            },
            ExpiredResponse(protocol_version=1, kind="expired"),
            DeniedResponse(protocol_version=1, kind="denied"),
        ]
    )
    tools = _tools(reader, first_page=_page(sequences=(1,), next_cursor="cursor-1"))
    await tools.timeline(_call(PAGE_TOOL), 1)

    statuses = []
    for step in range(3):
        result = await tools.query(
            _call(EVENT_TOOL, event_id="event-1", field="text"), step + 2
        )
        assert result.status == "success"
        statuses.append(result.output["status"])  # type: ignore[index,call-overload]

    assert statuses == ["absent", "expired", "denied"]
    assert tools.read_events() == {}


async def test_an_unreadable_field_does_not_end_the_run() -> None:
    reader = _FakeReader(evidence=[ReadTimeout()])
    tools = _tools(reader, first_page=_page(sequences=(1,), next_cursor="cursor-1"))
    await tools.timeline(_call(PAGE_TOOL), 1)

    result = await tools.query(_call(EVENT_TOOL, event_id="event-1", field="text"), 2)

    assert result.status == "error"
    assert "ZANEI_EVENT_UNREADABLE" in str(result.output)


async def test_read_events_joins_one_field_in_order_and_keeps_fields_apart() -> None:
    """Claimed records are verified against this text, so its seams are load bearing.

    The two halves of one field belong together however the model paged through
    them; two different fields do not, and an event that yielded no text at all
    must stay absent rather than verify against an empty string.
    """
    title = "設計レビューの記録" * 20
    head, tail, url = "前半の本文。", "後半の本文。", "https://example.test/a"
    head_bytes = len(head.encode("utf-8"))
    total = head_bytes + len(tail.encode("utf-8"))
    page = _page(sequences=(1, 2), next_cursor="cursor-1")
    reader = _FakeReader(
        evidence=[
            _evidence(start=head_bytes, end=total, total=total, text=tail),
            _evidence(start=0, end=head_bytes, total=total, text=head),
            _evidence(
                start=0,
                end=len(url.encode("utf-8")),
                total=len(url.encode("utf-8")),
                text=url,
            ),
        ]
    )
    tools = _tools(
        reader,
        first_page=PageResponse.model_validate(
            {
                **page.model_dump(),
                "observations": (
                    {
                        **_observation(1),
                        "window_title": {"kind": "value", "text": title},
                    },
                    _observation(2),
                ),
            }
        ),
    )
    await tools.timeline(_call(PAGE_TOOL), 1)

    await tools.query(
        _call(EVENT_TOOL, event_id="event-1", field="text", start=head_bytes), 2
    )
    await tools.query(_call(EVENT_TOOL, event_id="event-1", field="text"), 3)
    await tools.query(_call(EVENT_TOOL, event_id="event-1", field="url"), 4)

    events = tools.read_events()
    assert set(events) == {"event-1"}
    event = events["event-1"]
    assert event.texts == (head + tail, url)
    # Records keep the stored UTC time, not the local text the model read.
    assert event.observed_at == "2026-09-26T21:50:00Z"
    assert event.app_name == "Editor"
    assert event.bundle_id is None
    # The prompt page caps a title; verifying a source against the capped value
    # would reject records read from a longer one.
    assert len(title) > FREE_TEXT_CHARACTERS
    assert event.window_title == title


async def test_identifier_fields_are_projected_more_tightly_than_free_text() -> None:
    """Only titles and app names need room; the rest are enums and bundle ids."""
    long_value = {"kind": "value", "text": "あ" * 300}
    page = PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": (
                {
                    "append_sequence": 1,
                    "id": "event-1",
                    "ts": "2026-09-07T00:01:00Z",
                    "source": long_value,
                    "event_type": long_value,
                    "bundle_id": long_value,
                    "app_name": long_value,
                    "pid": None,
                    "window_title": long_value,
                    "window_id": None,
                },
            ),
            "next_cursor": "cursor-1",
            "upper_bound": "upper-1",
            "has_more": False,
            "coverage": {"after": 0, "through": 1},
        }
    )
    tools = _tools(_FakeReader(), first_page=page)

    result = await tools.timeline(_call(PAGE_TOOL), 1)

    event = result.output["contexts"][0]  # type: ignore[index,call-overload]
    free_text = "あ" * FREE_TEXT_CHARACTERS + " [truncated]"
    identifier = "あ" * IDENTIFIER_CHARACTERS + " [truncated]"
    assert event["window_title"] == free_text
    assert event["app"] == free_text
    assert event["event_type"] == identifier
    assert event["source"] == identifier
    assert event["bundle_id"] == identifier


@pytest.mark.usefixtures("tokyo_local_zone")
async def test_fifteen_minutes_keep_every_event_and_allow_detailed_reads() -> None:
    """75 observed events/minute must not outrun the 15-minute reader."""
    start = datetime(2026, 9, 7, tzinfo=UTC)
    pages = []
    expected = []
    for first in range(1, 1126, PAGE_LIMIT):
        sequences = tuple(range(first, min(first + PAGE_LIMIT, 1126)))
        observations = []
        for sequence in sequences:
            event = _observation(sequence)
            event["id"] = str(UUID(int=sequence))
            event["ts"] = (start + timedelta(seconds=sequence * 0.8)).isoformat()
            event["window_title"] = {
                "kind": "value",
                "text": "設計中のプロジェクト仕様書"
                if sequence != 563
                else "顧客から方針変更の連絡",
            }
            observations.append(event)
            expected.append((event["id"], describe_utc_timestamp(event["ts"])))
        page = _page(
            sequences=sequences,
            next_cursor=f"cursor-{sequences[-1]}",
            has_more=sequences[-1] < 1125,
        )
        pages.append(
            PageResponse.model_validate(
                {**page.model_dump(), "observations": tuple(observations)}
            )
        )
    reader = _FakeReader(
        pages=pages, evidence=[_evidence(start=0, end=6, total=6, text="変更")]
    )
    tools = _tools(reader)
    actual = []
    important = None
    while tools.has_more and not tools.page_budget_spent:
        result = await tools.timeline(_call(PAGE_TOOL), tools.pages_read + 1)
        output = result.output
        for event_id, timestamp, context_index in output["events"]:
            actual.append((event_id, timestamp))
            if event_id == str(UUID(int=563)):
                important = output["contexts"][context_index]["window_title"]
    assert actual == expected
    assert important == "顧客から方針変更の連絡"
    assert tools.has_more is False
    assert tools.timeline_chars < MAX_TIMELINE_CHARS_PER_RUN
    evidence = await tools.query(
        _call(EVENT_TOOL, event_id=str(UUID(int=563)), field="text"), 10
    )
    assert evidence.output["text"] == "変更"
    assert reader.evidence_requests[0].origin.event_id == str(UUID(int=563))


@pytest.mark.parametrize(
    "tail_start, expected",
    [(6, ("あいうえ",)), (9, ("あい", "え")), (3, ("あいうえ",))],
)
async def test_read_events_preserves_byte_gaps_and_overlaps(
    tail_start: int, expected: tuple[str, ...]
) -> None:
    tail = "あいうえ".encode()[tail_start:].decode()
    reader = _FakeReader(
        evidence=[
            _evidence(start=tail_start, end=12, total=12, text=tail),
            _evidence(start=0, end=6, total=12, text="あい"),
        ]
    )
    tools = _tools(reader, first_page=_page(sequences=(1,), next_cursor="cursor-1"))
    await tools.timeline(_call(PAGE_TOOL), 1)
    await tools.query(
        _call(EVENT_TOOL, event_id="event-1", field="text", start=tail_start), 2
    )
    await tools.query(_call(EVENT_TOOL, event_id="event-1", field="text"), 3)
    assert tools.read_events()["event-1"].texts == expected

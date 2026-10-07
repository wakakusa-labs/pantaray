from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from pantaray_agents.agents.insight_agent.agent import (
    SHORT_INSIGHT_RETRIEVAL_POLICY,
    _completion_rejection,
    _warn_if_range_unread,
)
from pantaray_agents.agents.insight_agent.zanei_tools import (
    FREE_TEXT_CHARACTERS,
    MAX_TIMELINE_CHARS_PER_RUN,
    MAX_TIMELINE_PAGES_PER_RUN,
    PAGE_LIMIT,
    PAGE_TOOL,
    ZaneiTools,
)
from pantaray_agents.local_runtime.context.source_gate import ActiveSource
from pantaray_agents.local_runtime.context.source_protocol import (
    PageReadRequest,
    PageResponse,
)
from pantaray_agents.local_runtime.tooling.memory_retrieval import (
    MEMORY_SEARCH_TOOL_NAME,
    MemoryContextSession,
    MemoryRetrievalSession,
)
from pantaray_agents.schema.context_source import SourceBinding
from pantaray_agents.tools.contract import ReactToolCall, ToolCallEnvelope

RUN = "run-1"
STORE_ID = "store-1"
GATE_LOGGER = "pantaray_agents.agents.insight_agent.agent"


_JAPANESE_PHRASES = (
    "作業内容の詳細な記録",
    "ウィンドウタイトルの取得",
    "設計レビューの指摘事項",
    "実装方針を再検討する",
    "テストが失敗した原因",
    "検証手順をまとめる",
    "本番環境への影響範囲",
    "会議の議事録を作成",
    "顧客からの問い合わせ",
    "性能測定の結果報告",
)


def _japanese(seed: int, length: int) -> str:
    """Ordinary Japanese prose, not one phrase repeated.

    A repeated phrase tokenizes far below real text, which would make a page
    built from it cheaper than the window titles this budget is sized for.
    """
    generator = random.Random(seed)
    text = ""
    while len(text) < length:
        text += generator.choice(_JAPANESE_PHRASES)
    return text[:length]


def _observation(sequence: int) -> dict[str, object]:
    absent: dict[str, object] = {"kind": "absent"}
    return {
        "append_sequence": sequence,
        "id": f"event-{sequence}",
        "ts": "2026-09-07T00:01:00Z",
        "source": absent,
        "event_type": absent,
        "bundle_id": absent,
        "app_name": {"kind": "value", "text": "Editor"},
        "pid": None,
        "window_title": absent,
        "window_id": None,
    }


def _dense_observation(sequence: int) -> dict[str, object]:
    """A row of the densest page the recorder can actually produce.

    Only the window title and the app display name carry free text; the other
    three projected fields are recorder enums and a reverse-DNS bundle id, so a
    page dense in all five is unreachable.
    """
    return {
        "append_sequence": sequence,
        "id": f"evt_01M1XRX9{sequence:022d}",
        "ts": "2026-09-07T00:01:00Z",
        "source": {"kind": "value", "text": "macos.accessibility"},
        "event_type": {"kind": "value", "text": "content.snapshot"},
        "bundle_id": {
            "kind": "value",
            "text": "com.example.very.long.reverse.dns.bundle.identifier",
        },
        "app_name": {
            "kind": "value",
            "text": _japanese(sequence, FREE_TEXT_CHARACTERS + 1),
        },
        "pid": None,
        "window_title": {
            "kind": "value",
            "text": _japanese(sequence + 10_000, FREE_TEXT_CHARACTERS + 1),
        },
        "window_id": 100_000 + sequence,
    }


def _page(
    *,
    sequences: tuple[int, ...],
    next_cursor: str,
    has_more: bool,
    dense: bool = False,
) -> PageResponse:
    build = _dense_observation if dense else _observation
    return PageResponse.model_validate(
        {
            "protocol_version": 1,
            "kind": "page",
            "store_identity": STORE_ID,
            "observations": tuple(build(sequence) for sequence in sequences),
            "next_cursor": next_cursor,
            "upper_bound": "upper-1",
            "has_more": has_more,
            "coverage": {"after": sequences[0] - 1, "through": sequences[-1]},
        }
    )


def _dense_page(*, first_sequence: int, next_cursor: str) -> PageResponse:
    return _page(
        sequences=tuple(range(first_sequence, first_sequence + PAGE_LIMIT)),
        next_cursor=next_cursor,
        has_more=True,
        dense=True,
    )


@dataclass
class _FakeReader:
    pages: list[PageResponse] = field(default_factory=list)

    async def read_page(
        self, _source: ActiveSource, _request: PageReadRequest
    ) -> PageResponse:
        return self.pages.pop(0)


def _tools(reader: _FakeReader, first_page: PageResponse) -> ZaneiTools:
    return ZaneiTools(
        reader=reader,  # type: ignore[arg-type]
        source=ActiveSource(
            binding=SourceBinding(
                user_id="user-1",
                epoch=UUID(int=1),
                policy_revision="policy-1",
                store_id=STORE_ID,
                protocol_version=1,
            )
        ),
        cursor=None,
        upper_bound="upper-1",
        first_page=first_page,
    )


def _timeline_call() -> ReactToolCall:
    return ReactToolCall(
        tool_name=PAGE_TOOL,
        tool_args={},
        tool_call_envelope=ToolCallEnvelope(tool_id=PAGE_TOOL, reason=None, args={}),
    )


async def test_completing_is_held_until_the_run_reads_its_whole_range() -> None:
    reader = _FakeReader(
        pages=[_page(sequences=(2,), next_cursor="cursor-2", has_more=False)]
    )
    tools = _tools(reader, _page(sequences=(1,), next_cursor="cursor-1", has_more=True))

    assert _completion_rejection(zanei=tools, final_turn=False) == (
        "Read zanei_timeline before completing."
    )

    await tools.timeline(_timeline_call(), 1)
    unread = _completion_rejection(zanei=tools, final_turn=False)
    assert unread is not None
    assert "has_more false" in unread

    await tools.timeline(_timeline_call(), 2)
    assert _completion_rejection(zanei=tools, final_turn=False) is None


async def test_a_spent_tool_budget_completes_the_partial_run_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reader = _FakeReader()
    tools = _tools(
        reader, _page(sequences=(1, 2), next_cursor="cursor-1", has_more=True)
    )
    await tools.timeline(_timeline_call(), 1)

    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        # The forced final turn is let through without a warning: the arguments
        # may still fail validation, and only an accepted completion is reported.
        assert _completion_rejection(zanei=tools, final_turn=True) is None
        assert caplog.records == []
        _warn_if_range_unread(run_id=RUN, zanei=tools)

    assert tools.cursor == "cursor-1"
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.run_id == RUN  # type: ignore[attr-defined]
    assert record.events_read == 2  # type: ignore[attr-defined]
    assert record.last_append_sequence == 2  # type: ignore[attr-defined]


async def test_a_drained_run_completes_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = _tools(
        _FakeReader(), _page(sequences=(1,), next_cursor="cursor-1", has_more=False)
    )
    await tools.timeline(_timeline_call(), 1)

    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        assert _completion_rejection(zanei=tools, final_turn=True) is None
        _warn_if_range_unread(run_id=RUN, zanei=tools)

    assert caplog.records == []


async def test_a_spent_page_budget_stops_the_drain_and_completes_the_partial_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A backlog larger than one run's transcript must not fail every retry."""
    reader = _FakeReader(
        pages=[
            _page(
                sequences=(sequence,), next_cursor=f"cursor-{sequence}", has_more=True
            )
            for sequence in range(2, MAX_TIMELINE_PAGES_PER_RUN + 2)
        ]
    )
    tools = _tools(reader, _page(sequences=(1,), next_cursor="cursor-1", has_more=True))

    for step in range(MAX_TIMELINE_PAGES_PER_RUN):
        last = step == MAX_TIMELINE_PAGES_PER_RUN - 1
        result = await tools.timeline(_timeline_call(), step + 1)
        output = result.output
        assert isinstance(output, dict)
        # The page that spends the budget says so, so the model does not waste a
        # tool call discovering it.
        assert ("page_budget_spent" in output) is last
        assert (_completion_rejection(zanei=tools, final_turn=False) is None) is last

    # The cap holds even though the range still has pages, and further calls read
    # nothing and say so instead of growing the transcript.
    assert tools.has_more is True
    assert tools.pages_read == MAX_TIMELINE_PAGES_PER_RUN
    spent = await tools.timeline(_timeline_call(), MAX_TIMELINE_PAGES_PER_RUN + 1)
    assert spent.output["page_budget_spent"] is True  # type: ignore[index,call-overload]
    assert spent.output["events"] == []  # type: ignore[index,call-overload]
    assert reader.pages  # the page beyond the cap was never read

    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        _warn_if_range_unread(run_id=RUN, zanei=tools)

    assert tools.cursor == f"cursor-{MAX_TIMELINE_PAGES_PER_RUN}"
    record = caplog.records[-1]
    assert record.pages_read == MAX_TIMELINE_PAGES_PER_RUN  # type: ignore[attr-defined]
    assert record.events_read == MAX_TIMELINE_PAGES_PER_RUN  # type: ignore[attr-defined]


async def test_a_dense_page_spends_the_character_budget_before_the_page_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The page count was sized from an average page; dense pages cost far more."""
    reader = _FakeReader(
        pages=[
            _dense_page(first_sequence=first, next_cursor=f"cursor-{index + 2}")
            for index, first in enumerate((201, 401))
        ]
    )
    tools = _tools(reader, _dense_page(first_sequence=1, next_cursor="cursor-1"))

    first = await tools.timeline(_timeline_call(), 1)
    assert "page_budget_spent" not in first.output  # type: ignore[operator]
    assert tools.timeline_chars < MAX_TIMELINE_CHARS_PER_RUN

    second = await tools.timeline(_timeline_call(), 2)

    # Dense pages must hit the character bound before the page-count bound.
    assert tools.timeline_chars >= MAX_TIMELINE_CHARS_PER_RUN
    assert tools.pages_read == 2 < MAX_TIMELINE_PAGES_PER_RUN
    assert second.output["page_budget_spent"] is True  # type: ignore[index,call-overload]
    assert _completion_rejection(zanei=tools, final_turn=False) is None

    spent = await tools.timeline(_timeline_call(), 3)
    assert spent.output["events"] == []  # type: ignore[index,call-overload]
    assert reader.pages  # the third dense page was never read

    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        _warn_if_range_unread(run_id=RUN, zanei=tools)

    record = caplog.records[-1]
    assert record.pages_read == 2  # type: ignore[attr-defined]
    assert record.timeline_chars == tools.timeline_chars  # type: ignore[attr-defined]


def test_the_short_insight_bounds_memory_content_per_call() -> None:
    """Unbounded memory bodies could push a drained run past the proxy input limit."""
    assert SHORT_INSIGHT_RETRIEVAL_POLICY.max_results == 4
    assert SHORT_INSIGHT_RETRIEVAL_POLICY.default_limit == 4
    assert SHORT_INSIGHT_RETRIEVAL_POLICY.search_content_max_chars == 1200
    assert SHORT_INSIGHT_RETRIEVAL_POLICY.reference_content_max_chars == 4000
    assert SHORT_INSIGHT_RETRIEVAL_POLICY.call_limit == 6


def test_the_short_insight_does_not_advertise_read_tools_it_cannot_offer(
    tmp_path: Path,
) -> None:
    """InsightAgent.generate registers no read-only file tools."""
    definitions = MemoryRetrievalSession(
        db_path=tmp_path / "runtime.sqlite3",
        busy_timeout_ms=1_000,
        context=MemoryContextSession(user_id="user-1", run_id=RUN),
        policy=SHORT_INSIGHT_RETRIEVAL_POLICY,
    ).definitions()

    search = next(item for item in definitions if item.name == MEMORY_SEARCH_TOOL_NAME)
    assert "read_root" not in search.description
    assert "read_path" not in search.description
    assert "cannot be expanded" in search.description

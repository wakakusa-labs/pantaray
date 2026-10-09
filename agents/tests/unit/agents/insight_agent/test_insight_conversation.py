"""The short Insight run on the shared conversation loop, driven by a fake model."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.unit.agents.insight_agent.test_completion_gate import (
    _FakeReader,
    _page,
    _tools,
)

from pantaray_agents.agents.insight_agent.agent import InsightAgent
from pantaray_agents.conversation import provider_turns
from pantaray_agents.utils.llm_types import GenerateContentConfig
from pantaray_llm.contracts.action_turn import (
    LlmActionTurnRequest,
    LlmActionTurnResponse,
)
from pantaray_llm.contracts.conversation import LlmTurnToolResultItem
from pantaray_llm.contracts.tool_use import LlmToolCall
from pantaray_llm.profiles import INSIGHT_PROFILE_ID

_OUTPUT = {
    "activity": "- Edited the report.",
    "insight": "Working on the report.",
    "reconsideration_reason": None,
    "records": [],
}


def _timeline(call_id: str) -> LlmToolCall:
    return LlmToolCall(call_id=call_id, name="zanei_timeline", arguments={})


def _completed(call_id: str) -> LlmToolCall:
    return LlmToolCall(call_id=call_id, name="completed", arguments=dict(_OUTPUT))


class _Models:
    def __init__(self, script: Sequence[LlmToolCall]) -> None:
        self.script = script
        self.requests: list[LlmActionTurnRequest] = []
        self.profiles: list[str] = []

    async def generate_content(self, **kwargs: object) -> object:
        config = kwargs["config"]
        assert isinstance(config, GenerateContentConfig)
        assert isinstance(config.tool_use, LlmActionTurnRequest)
        self.requests.append(config.tool_use)
        self.profiles.append(str(config.inference_profile))
        call = self.script[len(self.requests) - 1]
        return SimpleNamespace(
            action_turn=LlmActionTurnResponse(
                mode="action_turn", messages=[], calls=[call]
            ),
            provider_turn=None,
            usage_metadata=None,
        )


async def _generate(
    tmp_path: Path, script: Sequence[LlmToolCall], *, pages: int
) -> tuple[_Models, object]:
    models = _Models(script)
    agent = InsightAgent(
        client=SimpleNamespace(aio=SimpleNamespace(models=models)), llm_config={}
    )
    reader = _FakeReader(
        pages=[
            _page(sequences=(n,), next_cursor=f"cursor-{n}", has_more=n < pages)
            for n in range(2, pages + 1)
        ]
    )
    zanei = _tools(reader, _page(sequences=(1,), next_cursor="cursor-1", has_more=True))
    output = await agent.generate(
        run_id="run-1",
        user_id="user-1",
        zanei=zanei,
        workspace_context="(none)",
        previous_insight="(none)",
        db_path=tmp_path / "runtime.sqlite3",
        busy_timeout_ms=1_000,
    )
    return models, output


@pytest.fixture(autouse=True)
def _cloud_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "cloud")


async def test_completion_waits_for_the_whole_range_then_saves_the_outputs(
    tmp_path: Path,
) -> None:
    script = [
        _timeline("t1"),
        _completed("early"),
        _timeline("t2"),
        _completed("done"),
    ]

    models, output = await _generate(tmp_path, script, pages=2)

    assert output.activity == _OUTPUT["activity"]  # type: ignore[attr-defined]
    assert models.profiles == [INSIGHT_PROFILE_ID] * 4
    # Each request is the previous one plus what that turn added.
    sent = [request.conversation or [] for request in models.requests]
    assert all(
        later[: len(earlier)] == earlier
        for earlier, later in zip(sent, sent[1:], strict=False)
    )
    early = next(
        item
        for item in sent[-1]
        if isinstance(item, LlmTurnToolResultItem) and item.call_id == "early"
    )
    assert "has_more false" in str(early.output)


async def test_the_last_turn_accepts_a_completion_with_the_range_unread(
    tmp_path: Path,
) -> None:
    """Out of tool calls, the run completes instead of stranding its cursor."""

    unread = LlmToolCall(
        call_id="q", name="zanei_query", arguments={"event_id": "x", "field": "text"}
    )
    script = [
        _timeline("t1"),
        *(unread.model_copy(update={"call_id": f"q{n}"}) for n in range(35)),
        _completed("done"),
    ]

    models, output = await _generate(tmp_path, script, pages=5)

    assert output.insight == _OUTPUT["insight"]  # type: ignore[attr-defined]
    assert len(models.requests) == 37
    assert "This is the last turn" in str(models.requests[-1].conversation)

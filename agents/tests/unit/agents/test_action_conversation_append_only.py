"""What an Action hands its LLM client, turn after turn, through the real nodes."""

from __future__ import annotations

from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.unit.agents.test_action_agent_tool_batch_execution import (
    _act,
    _build_fixture,
    _think,
)

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm import turn_input
from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.conversation import provider_turns
from pantaray_agents.local_runtime.runtime.connection_store import ApiKeyConnection
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.llm_types import GenerateContentConfig
from pantaray_agents.utils.prompt_loader import PromptLoader
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.conversation import (
    LlmTurnAssistantItem,
    OpenAiProviderTurn,
)
from pantaray_llm.contracts.tool_use import LlmToolCall


def _provider_turn(call_id: str) -> OpenAiProviderTurn:
    return OpenAiProviderTurn(
        provider="openai",
        items=[
            {"type": "reasoning", "id": f"rs_{call_id}", "encrypted_content": "x"},
            {"type": "function_call", "call_id": call_id, "name": "read"},
        ],
    )


def _reply(call_id: str, tool: str, arguments: dict[str, JSONValue]) -> object:
    """A turn of one call; its commentary is folded into the item that carries it."""

    return SimpleNamespace(
        action_turn=LlmActionTurnResponse(
            mode="action_turn",
            messages=[
                LlmCommentary(phase="commentary", source_message_id=call_id, text="…")
            ],
            calls=[
                LlmToolCall(
                    call_id=call_id,
                    name=tool,
                    arguments={**arguments, STEP_NOTE_ARG: f"Running {call_id}."},
                )
            ],
        ),
        provider_turn=_provider_turn(call_id),
        usage_metadata={"prompt_tokens": 1000, "completion_tokens": 10},
    )


async def _run_four_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[GenerateContentConfig]:
    """Four THINKs that each read a file; the second is accepted on a retry."""

    agent, runtime, state, _ = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-append-only", allowed_tool_ids=("read",)
    )
    # The production template, which sends the history as conversation items.
    agent._executing_config = PromptLoader().load_config("action/executing")  # type: ignore[attr-defined]
    connection = ApiKeyConnection(provider="openai", model="m", api_key="sk-not-real")
    monkeypatch.setattr(provider_turns, "read_llm_route", lambda: "direct")
    monkeypatch.setattr(provider_turns, "peek_llm_connection", lambda: connection)
    paths = []
    for name in ("a", "b", "c", "d"):
        path = Path(state["action_temp_dir"]) / f"{name}.txt"
        path.write_text(f"content {name}", encoding="utf-8")
        paths.append(str(path))
    replies = [
        _reply("call_a", "read", {"path": paths[0]}),
        _reply("bad", "not_a_tool", {}),
        _reply("call_b", "read", {"path": paths[1]}),
        _reply("call_c", "read", {"path": paths[2]}),
        _reply("call_d", "read", {"path": paths[3]}),
    ]
    sent: list[GenerateContentConfig] = []
    heads: set[str] = set()

    async def generate_content(
        *, contents: list[object], config: GenerateContentConfig
    ) -> object:
        heads.add(str(contents))
        sent.append(config)
        return replies.pop(0)

    monkeypatch.setattr(agent.client.aio.models, "generate_content", generate_content)
    # The clock moves every turn, so each one appends its own turn context and
    # every later turn must replay the ones recorded before it.
    clock = (f"2026-10-08 12:0{minute}" for minute in count())
    with patch.object(turn_input, "local_now_for_model", side_effect=clock):
        for _ in range(4):
            state = await _think(agent, runtime, state)
            assert state["status"] == "processing"
            state = await _act(agent, runtime, state)
    assert not replies
    # The request's own message is the head, which every turn sends unchanged.
    assert len(heads) == 1
    return sent


def _items(config: GenerateContentConfig) -> list[object]:
    assert config.tool_use is not None
    return list(config.tool_use.conversation or ())


@pytest.mark.asyncio
async def test_each_turn_sends_the_previous_request_with_items_appended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prompt cache reads a request only as far as it matches the last one."""

    sent = await _run_four_turns(monkeypatch, tmp_path)
    first_attempts = [sent[0], sent[1], sent[3], sent[4]]

    for before, after in zip(first_attempts, first_attempts[1:], strict=False):
        assert after.system_instruction == before.system_instruction
        assert after.tool_use is not None and before.tool_use is not None
        assert after.tool_use.tools == before.tool_use.tools
        earlier, later = _items(before), _items(after)
        assert len(later) > len(earlier)
        assert later[: len(earlier)] == earlier
    # A retry appends its notice to the attempt it repairs.
    assert _items(sent[2])[:-1] == _items(sent[1])


@pytest.mark.asyncio
async def test_a_turn_goes_back_only_behind_the_prefix_it_was_produced_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Appends keep every turn on its item; a notice no later turn replays does not."""

    sent = await _run_four_turns(monkeypatch, tmp_path)

    turn_a, turn_c = _provider_turn("call_a"), _provider_turn("call_c")
    assert [
        [
            item.provider_turn
            for item in _items(config)
            if isinstance(item, LlmTurnAssistantItem)
        ]
        for config in (sent[0], sent[1], sent[3], sent[4])
    ] == [
        [],
        [turn_a],
        # call_b was accepted behind a repair notice that no later turn sends.
        [turn_a, None],
        [turn_a, None, turn_c],
    ]

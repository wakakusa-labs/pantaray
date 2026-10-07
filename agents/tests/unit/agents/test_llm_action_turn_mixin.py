from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.test_llm_token_sink_contract import (
    _create_state_sink,
    _Host,
    _Models,
    _tool_definition,
)

from pantaray_agents.agents.core import CountingSink, TokenBudgetExceeded
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    LlmToolUseResponseError,
)
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.conversation import OpenAiProviderTurn
from pantaray_llm.errors import LlmProxyExecutionError


def _response():
    return SimpleNamespace(
        action_turn=LlmActionTurnResponse.model_validate(
            {
                "mode": "action_turn",
                "messages": [
                    {
                        "phase": "commentary",
                        "source_message_id": "msg-1",
                        "text": "確認します。",
                    }
                ],
                "calls": [],
            }
        ),
        usage_metadata={"prompt_tokens": 2, "completion_tokens": 3},
    )


@pytest.mark.asyncio
async def test_commentary_only_preserves_messages_and_accounts_for_usage() -> None:
    models = SimpleNamespace(generate_content=AsyncMock(return_value=_response()))
    host = _Host(models)
    sink = CountingSink()

    turn = await host._generate_llm_action_turn(
        sink=sink,
        prompt="test",
        tools=(_tool_definition(),),
        max_parallel_tool_calls=3,
    )

    assert [message.text for message in turn.response.messages] == ["確認します。"]
    assert turn.response.calls == []
    assert (sink.delta.prompt_tokens, sink.delta.completion_tokens) == (2, 3)
    request = models.generate_content.call_args.kwargs["config"].tool_use
    assert request.mode == "action_turn"
    assert request.max_parallel_tool_calls == 3
    assert [tool.name for tool in request.tools] == ["done"]


@pytest.mark.asyncio
async def test_the_provider_turn_comes_back_with_its_own_turn_only() -> None:
    provider_turn = OpenAiProviderTurn(provider="openai", items=[{"type": "x"}])
    typed, untyped = _response(), _response()
    typed.provider_turn = provider_turn
    untyped.provider_turn = provider_turn.model_dump(mode="json")
    host = _Host(_Models([typed, untyped, _response()]))

    replies = [
        await host._generate_llm_action_turn(
            sink=CountingSink(), prompt="test", tools=(_tool_definition(),)
        )
        for _ in range(3)
    ]

    assert [reply.provider_turn for reply in replies] == [provider_turn, None, None]


@pytest.mark.asyncio
async def test_retry_usage_is_included_in_action_turn_total() -> None:
    error = LlmProxyExecutionError(
        error_code="PROXY_LLM_UPSTREAM_ERROR",
        error_message="temporary failure",
        retryable=True,
        usage_metadata={"prompt_tokens": 5, "completion_tokens": 1},
    )
    host = _Host(_Models([error, _response()]))
    sink = CountingSink()

    await host._generate_llm_action_turn(
        sink=sink, prompt="test", tools=(_tool_definition(),)
    )

    assert (sink.delta.prompt_tokens, sink.delta.completion_tokens) == (7, 4)


@pytest.mark.asyncio
async def test_repair_next_turn_propagates_without_another_http_attempt() -> None:
    error = LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="invalid phase",
        retryable=False,
        recovery="repair_next_turn",
        usage_metadata={"prompt_tokens": 4, "completion_tokens": 2},
    )
    models = _Models([error, _response()])
    host = _Host(models)
    sink = CountingSink()

    with pytest.raises(LlmProxyExecutionError) as caught:
        await host._generate_llm_action_turn(
            sink=sink, prompt="test", tools=(_tool_definition(),)
        )

    assert caught.value is error
    assert models.await_count == 1
    assert (sink.delta.prompt_tokens, sink.delta.completion_tokens) == (4, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("action_turn", [None, {"mode": "action_turn"}])
async def test_untyped_action_response_fails_after_accounting_for_usage(
    action_turn: object,
) -> None:
    response = _response()
    response.action_turn = action_turn
    host = _Host(_Models([response]))
    sink = CountingSink()

    with pytest.raises(LlmToolUseResponseError, match="no Action turn"):
        await host._generate_llm_action_turn(
            sink=sink, prompt="test", tools=(_tool_definition(),)
        )

    assert (sink.delta.prompt_tokens, sink.delta.completion_tokens) == (2, 3)


@pytest.mark.asyncio
async def test_commentary_only_budget_exhaustion_blocks_next_http_call() -> None:
    models = _Models([_response(), _response()])
    host = _Host(models)
    sink = _create_state_sink(token_budget=4)

    for _attempt in range(2):
        with pytest.raises(TokenBudgetExceeded):
            await host._generate_llm_action_turn(
                sink=sink, prompt="test", tools=(_tool_definition(),)
            )

    assert sink.state["tokens_used"] == 5
    assert models.await_count == 1

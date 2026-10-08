from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest

from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.services.token_accounting_service import (
    ActionTokenAccountingService,
    StateTokenSink,
    TokenAccountingDeps,
)
from pantaray_agents.agents.core import (
    CountingSink,
    LlmUsage,
    TokenBudgetExceeded,
    TokenSink,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import (
    LlmToolUseMixin,
    LlmToolUseResponseError,
)
from pantaray_agents.schema.agent.base import AgentError
from pantaray_llm.contracts.tool_use import LlmToolCall, LlmToolDefinition
from pantaray_llm.errors import LlmProxyExecutionError


def _build_agent_error(**values: object) -> AgentError:
    values.setdefault("severity", "error")
    return AgentError.model_validate(values)


def _create_state(*, token_budget: int | None = None):
    return create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-08-23T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=token_budget,
    )


def _create_state_sink(*, token_budget: int | None = None) -> StateTokenSink:
    service = ActionTokenAccountingService(
        TokenAccountingDeps(
            build_agent_error=_build_agent_error,
        )
    )
    return StateTokenSink(service, _create_state(token_budget=token_budget))


def test_counting_sink_implements_required_contract() -> None:
    sink: TokenSink = CountingSink()

    sink.guard()
    sink.record(
        LlmUsage(3, 5, {"prompt_token_count": 3, "output_token_count": 5}),
        stage="test",
        may_raise=True,
    )
    sink.record(
        LlmUsage(None, None, {}),
        stage=None,
        may_raise=True,
    )

    assert isinstance(sink, CountingSink)
    assert sink.delta == LlmUsage(
        3,
        5,
        {"prompt_token_count": 3, "output_token_count": 5},
    )


def test_state_sink_sticky_guard_blocks_after_budget_exhaustion() -> None:
    sink = _create_state_sink(token_budget=4)

    with pytest.raises(TokenBudgetExceeded):
        sink.record(
            LlmUsage(3, 2, {"prompt_token_count": 3, "output_token_count": 2}),
            stage="planning",
            may_raise=True,
        )

    assert sink.state["tokens_used"] == 5
    assert sink.state["status"] == "error"
    assert sink.budget_error is not None
    with pytest.raises(TokenBudgetExceeded):
        sink.guard()


def test_state_sink_records_sticky_budget_error_without_masking_caller_error() -> None:
    sink = _create_state_sink(token_budget=1)

    sink.record(
        LlmUsage(1, 1, {}),
        stage="planning",
        may_raise=False,
    )

    assert sink.budget_error is not None
    assert sink.delta == LlmUsage(1, 1, {})
    with pytest.raises(TokenBudgetExceeded):
        sink.guard()


def test_state_sink_rebind_preserves_accounting_without_double_adding_delta() -> None:
    sink = _create_state_sink(token_budget=4)
    usage = LlmUsage(
        3,
        2,
        {"prompt_token_count": 3, "completion_token_count": 2},
    )

    sink.record(usage, stage="tool::thinking", may_raise=False)
    replacement = copy.deepcopy(sink.state)
    sink.rebind_state(replacement)

    assert replacement["total_prompt_tokens"] == 3
    assert replacement["total_completion_tokens"] == 2
    assert replacement["tokens_used"] == 5
    assert replacement["status"] == "error"
    assert [error["error_code"] for error in replacement["errors"]].count(
        "ACTION_TOKEN_BUDGET_EXCEEDED"
    ) == 1
    assert sink.delta == usage

    second_replacement = copy.deepcopy(replacement)
    sink.rebind_state(second_replacement)

    assert second_replacement["tokens_used"] == 5
    assert [error["error_code"] for error in second_replacement["errors"]].count(
        "ACTION_TOKEN_BUDGET_EXCEEDED"
    ) == 1
    assert sink.delta == usage


class _RecordingSink(CountingSink):
    def __init__(self) -> None:
        super().__init__()
        self.record_count = 0

    def record(
        self,
        usage: LlmUsage,
        *,
        stage: str | None,
        may_raise: bool,
    ) -> None:
        self.record_count += 1
        super().record(usage, stage=stage, may_raise=may_raise)


class _Response:
    def __init__(
        self,
        *,
        text: str = "ok",
        usage_metadata: object | None = None,
        tool_calls: tuple[LlmToolCall, ...] = (),
    ) -> None:
        self.text = text
        self.usage_metadata = usage_metadata
        self.tool_calls = tool_calls
        self.dropped_tool_call_names: tuple[str, ...] = ()
        self.tool_continuation = None


class _Models:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.await_count = 0

    async def generate_content(self, **_kwargs: object) -> object:
        self.await_count += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Aio:
    def __init__(self, models: object) -> None:
        self.models = models


class _Client:
    def __init__(self, models: object) -> None:
        self.aio = _Aio(models)


class _Host(LlmToolUseMixin):
    DEFAULT_SYSTEM_INSTRUCTION = "test"
    LLM_INFERENCE_PROFILE_ID = "test.profile"

    def __init__(self, models: object) -> None:
        self.client = _Client(models)
        self.llm_config = {}

    async def _sleep_llm_retry_backoff(self, attempt: int) -> None:
        del attempt


def _tool_definition() -> LlmToolDefinition:
    return LlmToolDefinition(
        name="done",
        description="Finish",
        parameters={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_generate_records_once_on_success() -> None:
    sink = _RecordingSink()
    host = _Host(
        _Models([_Response(usage_metadata={"prompt_tokens": 2, "output_tokens": 3})])
    )

    assert await host._generate_llm_response(prompt="test", sink=sink) == "ok"
    assert sink.record_count == 1
    assert sink.delta.prompt_tokens == 2
    assert sink.delta.completion_tokens == 3


@pytest.mark.asyncio
async def test_generate_records_once_on_permanent_failure() -> None:
    sink = _RecordingSink()
    host = _Host(_Models([ValueError("permanent")]))

    with pytest.raises(RuntimeError, match="permanent"):
        await host._generate_llm_response(prompt="test", sink=sink)

    assert sink.record_count == 1
    assert sink.delta == LlmUsage(None, None, {})


@pytest.mark.asyncio
async def test_generate_records_once_after_retry_success() -> None:
    sink = _RecordingSink()
    retry_error = LlmProxyExecutionError(
        error_code="PROXY_UPSTREAM_UNAVAILABLE",
        error_message="retry",
        retryable=True,
        usage_metadata={"prompt_tokens": 2, "completion_tokens": 1},
    )
    host = _Host(
        _Models(
            [
                retry_error,
                _Response(usage_metadata={"prompt_tokens": 5, "completion_tokens": 3}),
            ]
        )
    )

    assert await host._generate_llm_response(prompt="test", sink=sink) == "ok"
    assert sink.record_count == 1


@pytest.mark.asyncio
async def test_retry_records_sum_of_all_attempt_usage() -> None:
    sink = _RecordingSink()
    retry_error = LlmProxyExecutionError(
        error_code="PROXY_UPSTREAM_UNAVAILABLE",
        error_message="retry",
        retryable=True,
        usage_metadata={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    )
    host = _Host(
        _Models(
            [
                retry_error,
                retry_error,
                _Response(
                    usage_metadata={
                        "prompt_tokens": 5,
                        "completion_tokens": 3,
                        "total_tokens": 8,
                    }
                ),
            ]
        )
    )

    await host._generate_llm_response(prompt="test", sink=sink)

    assert sink.delta == LlmUsage(
        prompt_tokens=9,
        completion_tokens=5,
        fields={"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
    )
    assert sink.record_count == 1


@pytest.mark.asyncio
async def test_tool_call_records_once_for_repair_next_turn() -> None:
    sink = _RecordingSink()
    repair_error = LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="repair",
        retryable=False,
        recovery="repair_next_turn",
        usage_metadata={"prompt_tokens": 4, "completion_tokens": 2},
    )
    host = _Host(_Models([repair_error]))

    with pytest.raises(LlmProxyExecutionError):
        await host._generate_llm_tool_call(
            sink=sink,
            prompt="test",
            tools=(_tool_definition(),),
        )

    assert sink.record_count == 1
    assert sink.delta.prompt_tokens == 4
    assert sink.delta.completion_tokens == 2


@pytest.mark.asyncio
async def test_tool_call_records_once_for_response_validation_failure() -> None:
    sink = _RecordingSink()
    host = _Host(
        _Models([_Response(usage_metadata={"prompt_tokens": 1, "output_tokens": 2})])
    )

    with pytest.raises(LlmToolUseResponseError):
        await host._generate_llm_tool_call(
            sink=sink,
            prompt="test",
            tools=(_tool_definition(),),
        )

    assert sink.record_count == 1
    assert sink.delta.prompt_tokens == 1
    assert sink.delta.completion_tokens == 2


@pytest.mark.asyncio
async def test_missing_usage_records_empty_usage_once() -> None:
    sink = _RecordingSink()
    host = _Host(_Models([_Response(usage_metadata=None)]))

    await host._generate_llm_response(prompt="test", sink=sink)

    assert sink.record_count == 1
    assert sink.delta == LlmUsage(None, None, {})


@pytest.mark.asyncio
async def test_state_sink_sticky_guard_blocks_next_client_call() -> None:
    sink = _create_state_sink(token_budget=1)
    repair_error = LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="repair",
        retryable=False,
        recovery="repair_next_turn",
        usage_metadata={"prompt_tokens": 2, "completion_tokens": 1},
    )
    models = _Models([repair_error, MagicMock()])
    host = _Host(models)

    for _attempt in range(2):
        try:
            await host._generate_llm_tool_call(
                sink=sink,
                prompt="test",
                tools=(_tool_definition(),),
            )
        except LlmProxyExecutionError:
            continue
        except TokenBudgetExceeded:
            break

    assert models.await_count == 1

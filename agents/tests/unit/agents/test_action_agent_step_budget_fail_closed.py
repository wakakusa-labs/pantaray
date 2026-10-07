from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import create_state_token_sink

from pantaray_agents.agents.action_agent.agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    action_step,
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    build_next_action,
    build_tool_call,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    CounterInvariantError,
)
from pantaray_agents.schema.agent.base import AgentError


class _DummyAgent:
    """step budget の fail-closed 動作を検証するための最小エージェント。"""

    def __init__(self) -> None:
        self._build_agent_error = MagicMock(
            side_effect=lambda **kwargs: AgentError(
                severity="error",
                metadata=None,
                **kwargs,
            )
        )


def _make_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.services = SimpleNamespace(
        cancellation=SimpleNamespace(check_cancellation=AsyncMock(return_value=False)),
        response=SimpleNamespace(
            build_agent_error=MagicMock(
                side_effect=lambda **kwargs: AgentError(
                    severity="error",
                    metadata=None,
                    **kwargs,
                )
            )
        ),
    )
    return runtime


def test_coerce_action_token_budget_allows_missing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """action header の token_budget が欠落していれば None を返す。"""
    agent = object.__new__(ActionAgent)
    assert agent._coerce_action_token_budget(None) is None
    assert agent._coerce_action_token_budget("") is None


def test_coerce_action_token_budget_requires_positive_integer() -> None:
    """action header の token_budget は正の整数でなければ fail-fast にする。"""
    agent = object.__new__(ActionAgent)

    with pytest.raises(
        RuntimeError,
        match="Action header token_budget must be an integer",
    ):
        agent._coerce_action_token_budget("invalid")

    with pytest.raises(
        RuntimeError,
        match="Action header token_budget must be a positive integer",
    ):
        agent._coerce_action_token_budget("0")

    with pytest.raises(
        RuntimeError,
        match="Action header token_budget must be a positive integer",
    ):
        agent._coerce_action_token_budget("-1")


def test_coerce_action_token_budget_accepts_positive_integer() -> None:
    """正の整数の token budget はそのまま採用する。"""
    agent = object.__new__(ActionAgent)

    assert agent._coerce_action_token_budget("123") == 123


@pytest.mark.asyncio
async def test_execution_think_step_requires_max_steps() -> None:
    """max_steps が欠落している場合、暗黙フォールバックせず例外で停止する。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "step": 1,
        # NOTE: 意図的に max_steps/max_tool_steps を欠落させる
        "llm_steps_taken": 0,
        "tool_steps_taken": 0,
    }

    with pytest.raises(KeyError, match="max_steps"):
        await execution_think_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_execution_think_step_requires_max_tool_steps() -> None:
    """max_tool_steps が欠落している場合、暗黙フォールバックせず例外で停止する。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "step": 1,
        "max_steps": 10,
        # NOTE: 意図的に max_tool_steps を欠落させる
        "llm_steps_taken": 0,
        "tool_steps_taken": 0,
    }

    with pytest.raises(KeyError, match="max_tool_steps"):
        await execution_think_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_action_step_requires_step_budgets() -> None:
    """ACTION ノードでも step budget は必須で、欠落は例外で停止する。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "context": {"use_goal_workers": False},
        "step": 1,
        # NOTE: tool_def 解決までは通す（budget check まで到達させる）
        "next_action": build_next_action(
            tool=build_tool_call(
                tool_id="write_session_memory",
                args={"content": "notes"},
            ),
            decided_at="2025-01-01T00:00:00Z",
        ),
        # NOTE: 意図的に max_steps/max_tool_steps を欠落させる
        "tool_steps_taken": 0,
    }

    with pytest.raises(KeyError, match="max_tool_steps"):
        await action_step(agent, state, runtime, sink=create_state_token_sink(state))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_execution_think_step_rejects_missing_llm_counter() -> None:
    """llm_steps_taken 欠損は 0 補完せず不変条件違反で停止する。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "step": 1,
        "max_steps": 10,
        "max_tool_steps": 10,
        # NOTE: llm_steps_taken を意図的に欠損させる
        "tool_steps_taken": 0,
    }

    with pytest.raises(CounterInvariantError, match="llm_steps_taken is missing"):
        await execution_think_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_action_step_rejects_missing_tool_counter() -> None:
    """tool_steps_taken 欠損は 0 補完せず不変条件違反で停止する。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "context": {"use_goal_workers": False},
        "step": 1,
        "max_tool_steps": 10,
        "next_action": build_next_action(
            tool=build_tool_call(
                tool_id="write_session_memory",
                args={"content": "notes"},
            ),
            decided_at="2025-01-01T00:00:00Z",
        ),
        # NOTE: tool_steps_taken を意図的に欠損させる
    }

    with pytest.raises(CounterInvariantError, match="tool_steps_taken is missing"):
        await action_step(agent, state, runtime, sink=create_state_token_sink(state))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_execution_think_timeout_updates_updated_at() -> None:
    """budget timeout で早期 return する経路でも updated_at が更新されること。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "step": 1,
        "max_steps": 1,
        "max_tool_steps": 10,
        "llm_steps_taken": 1,
        "tool_steps_taken": 0,
        "updated_at": "2025-01-01T00:00:00Z",
    }

    updated = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    assert updated["status"] == "error"
    assert updated["next_action"] is None
    assert updated["updated_at"] != "2025-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_action_timeout_updates_updated_at() -> None:
    """tool budget timeout で早期 return する経路でも updated_at が更新されること。"""
    agent = _DummyAgent()
    runtime = _make_runtime()

    state = {
        "status": "processing",
        "phase": "executing",
        "context": {"use_goal_workers": False},
        "step": 1,
        "max_tool_steps": 1,
        "tool_steps_taken": 1,
        "next_action": build_next_action(
            tool=build_tool_call(
                tool_id="write_session_memory",
                args={"content": "notes"},
            ),
            decided_at="2025-01-01T00:00:00Z",
        ),
        "updated_at": "2025-01-01T00:00:00Z",
    }

    updated = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    assert updated["status"] == "error"
    assert updated["next_action"] is None
    assert updated["updated_at"] != "2025-01-01T00:00:00Z"

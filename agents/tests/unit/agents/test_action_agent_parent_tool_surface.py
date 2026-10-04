from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    project_request_user_step,
)
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_llm.contracts.tool_use import LlmToolDefinition
from pantaray_llm.profiles import (
    ACTION_EXECUTING_PROFILE_ID,
)

_GOAL_WORKER_TOOL_IDS = frozenset(
    {
        "run_next_goal",
        "send_message_to_goal_worker",
        "send_message_to_supervisor",
        "propose_goal_completion",
        "review_goal_completion",
    }
)
_HARD_PLAN_TOOL_IDS = frozenset(
    {
        "initialize_plan",
        "add_goal",
        "add_requirement",
        "close_goal",
        "close_requirement",
        "update_check_item_status",
    }
)
_CHILD_TOOL_IDS = frozenset(
    {
        "spawn_subagent",
        "send_message_to_subagent",
        "wait_subagents",
        "cancel_subagent",
    }
)
_GOAL_WORKER_PROMPT_MARKERS = (
    "goal worker",
    "goal_worker",
    "run_next_goal",
    "send_message_to_goal_worker",
    "send_message_to_supervisor",
    "propose_goal_completion",
    "review_goal_completion",
    "pending goal completion evidence",
)


@dataclass
class _CapturedParentSurface:
    prompt: str = ""
    system_instruction: str = ""
    tool_ids: tuple[str, ...] = ()
    stage: str | None = None
    profile_id: str | None = None


@pytest.mark.asyncio
async def test_parent_execution_surface_offers_children_without_legacy_tools() -> None:
    agent = ActionAgent(
        config={"llm_client": MockLLMClient(), "llm": {}},
        repository=MockActionAgentRepository(),
    )
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]

    request = build_action_request(
        action_id="act-parent-surface",
        suggestion_id="sug-parent-surface",
        user_id="user-parent-surface",
    )

    async def noop(_event: object) -> None:
        return None

    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 10,
            "max_tool_steps": 10,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
        },
        emit_action_step=noop,
        emit_error=noop,
        services=agent.runtime_services,
    )
    state = create_initial_state(
        user_id=request.user_id,
        suggestion_id=request.suggestion_id,
        action_id=request.action_id,
        started_at=datetime.now(UTC).isoformat(),
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state = project_request_user_step(state, request)
    state["phase"] = "executing"

    captured = _CapturedParentSurface()

    async def capture_parent_llm_call(
        *,
        prompt: str,
        tools: tuple[LlmToolDefinition, ...],
        system_instruction: str,
        stage: str | None,
        **_: object,
    ):
        captured.prompt = prompt
        captured.system_instruction = system_instruction
        captured.tool_ids = tuple(tool.name for tool in tools)
        captured.stage = stage
        captured.profile_id = agent._resolve_inference_profile_id(  # noqa: SLF001
            stage=stage
        )
        return native_tool_turn("thinking", {"query": "Decide the next step"})

    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[method-assign]
        side_effect=capture_parent_llm_call
    )

    await execution_think_step(
        agent,
        state,
        runtime,
        sink=create_state_token_sink(state),
    )

    rendered_prompt = f"{captured.system_instruction}\n{captured.prompt}".lower()
    assert all(marker not in rendered_prompt for marker in _GOAL_WORKER_PROMPT_MARKERS)
    assert _GOAL_WORKER_TOOL_IDS.isdisjoint(captured.tool_ids)
    assert _HARD_PLAN_TOOL_IDS.isdisjoint(captured.tool_ids)
    assert _CHILD_TOOL_IDS.issubset(captured.tool_ids)
    assert "Plan Rules" not in captured.system_instruction
    assert "plan.md" in captured.system_instruction
    assert captured.stage == "executing"
    assert captured.profile_id == ACTION_EXECUTING_PROFILE_ID
    # 送出した Supervisor プロンプトは Action 全体の最終 LLM 入力として保存される。
    # 会話の項目で送る turn では、要求そのものが運ぶのは固定の前半だけなので、
    # 保存されるのは同じ入力を 1 本の文字列に直した全体になる。
    assert state["context"]["last_supervisor_prompt"].startswith(captured.prompt)

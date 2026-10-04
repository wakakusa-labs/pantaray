from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    project_request_user_step,
)
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    action_step,
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.utils.prompt_loader import PromptConfig


def _runtime_services(agent: ActionAgent):
    return agent._runtime_services  # noqa: SLF001


def _make_agent() -> ActionAgent:
    """テスト用に最小プロンプトで ActionAgent を構築する。"""
    repo = MockActionAgentRepository()

    def _fake_load_config(_prompt_name: str) -> PromptConfig:
        # ここでは prompt 内容は重要ではない（LLM応答をモックする）
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        return ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}}, repository=repo
        )


@pytest.mark.asyncio
async def test_step_number_is_shared_between_think_and_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """THINK と ACTION が同じ step_number を共有し、ACTION 完了で進むこと。"""
    agent = _make_agent()
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id="act-stepnum",
        suggestion_id="sug-stepnum",
        user_id="user-stepnum",
    )
    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 10,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
        },
        emit_action_step=noop,
        emit_error=noop,
        services=_runtime_services(agent),
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
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=("read",),
    )
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    state["context"]["desires"] = [{"id": "D1", "type": "explicit", "content": "D"}]
    state["context"]["check_items"] = [{"id": "C1", "title": "T", "status": "open"}]
    state["context"]["requirements"] = [
        {
            "id": "R1",
            "goal_id": "G1",
            "content": "R",
            "status": "open",
            "dependencies": [],
        }
    ]

    # THINK が read_action_plan を選択
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=native_tool_turn("read_action_plan", {})
    )

    after_think = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    assert after_think["step"] == 2, "THINK では step を進めない（ACTION 完了で進める）"
    assert after_think["next_action"] and after_think["next_action"]["tool"]

    after_action = await action_step(
        agent, after_think, runtime, sink=create_state_token_sink(after_think)
    )  # type: ignore[arg-type]
    assert after_action["step"] == 3, "ACTION 完了で step が +1 される"

    supervisor_history = after_action["history_by_scope"]["S"]
    think_entry = next(
        entry
        for entry in supervisor_history
        if entry.get("step_type") == StepType.LLM_OUTPUT
    )
    action_entry = next(
        entry
        for entry in supervisor_history
        if entry.get("step_type") == StepType.TOOL_EXECUTION
    )
    assert think_entry["phase"] == action_entry["phase"] == "executing"
    assert think_entry["step_number"] == action_entry["step_number"] == 2

    # 永続化される step_number も一致する（THINK と tool::read_action_plan）
    assert agent.repository.save_action_step.await_count == 2  # type: ignore[attr-defined]
    first = agent.repository.save_action_step.call_args_list[0].kwargs  # type: ignore[attr-defined]
    second = agent.repository.save_action_step.call_args_list[1].kwargs  # type: ignore[attr-defined]
    assert first["step_number"] == 2
    assert second["step_number"] == 2

    # THINK の save_action_step が goal_handle="S" を持つこと。
    assert first["goal_handle"] == "S"
    # 同一論理ステップで THINK/TOOL が同じ local_step_number を共有する。
    assert first["local_step_number"] == second["local_step_number"]
    assert first["short_step_id"] == "S-2-THINK"
    assert second["short_step_id"] == "S-2-TOOL"

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.utils.prompt_loader import PromptConfig


def _runtime_services(agent: ActionAgent):
    return agent._runtime_services  # noqa: SLF001


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    """MockRepository のグローバルデータをクリアする（テスト独立性）。"""

    MockRepository.clear_data()


def _make_agent() -> ActionAgent:
    """MetaThink/Act のループを回すための最小 ActionAgent を作る。"""

    repo = MockActionAgentRepository()

    def _fake_load_config(_prompt_name: str) -> PromptConfig:
        # execution THINK は .format(...) するため、最低限 current_time があれば成立する。
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=repo,
        )
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent._consume_llm_thoughts = MagicMock(return_value=None)  # type: ignore[attr-defined]
    return agent


async def _seed_action_header(
    agent: ActionAgent,
    *,
    action_id: str,
    suggestion_id: str,
    user_id: str,
) -> None:
    await agent.repository.save_action(
        {
            "action_id": action_id,
            "suggestion_id": suggestion_id,
            "user_id": user_id,
            "final_output": "",
            "status": "processing",
            "prompt_name": "action/executing",
            "prompt_version": "test",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        prompt_name="action/executing",
        prompt_version="test",
    )


@pytest.mark.asyncio
async def test_supervisor_retries_tool_validation_error_up_to_five_then_aborts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Supervisor retries five consecutive validation failures before aborting."""

    agent = _make_agent()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    emit_error = AsyncMock()
    request = build_action_request(
        action_id="act-validation-retry",
        suggestion_id="sug-validation-retry",
        user_id="user-validation-retry",
    )
    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 20,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
        },
        emit_action_step=noop,
        emit_error=emit_error,
        services=_runtime_services(agent),
    )

    state = create_initial_state(
        user_id=request.user_id,
        suggestion_id=request.suggestion_id,
        action_id=request.action_id,
        started_at=datetime.now(UTC).isoformat(),
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False
    install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=("memory_search",),
    )
    await _seed_action_header(
        agent,
        action_id=request.action_id,
        suggestion_id=request.suggestion_id,
        user_id=request.user_id,
    )

    invalid_tool_call = native_tool_turn("memory_search", {"query": ""})
    agent._generate_llm_action_turn = AsyncMock(side_effect=[invalid_tool_call] * 5)  # type: ignore[attr-defined]

    # First through fourth failures remain internal to the repair loop.
    for i in range(4):
        state = await execution_think_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )  # type: ignore[arg-type]
        state = await action_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )  # type: ignore[arg-type]
        assert state["status"] == "processing"
        assert state["context"]["tool_validation_error_streak"] == i + 1

    # Fifth consecutive failure is terminal.
    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    state = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    assert state["context"]["tool_validation_error_streak"] == 5
    assert state["status"] == "error"
    assert state.get("final_output") in ("", None)
    assert state.get("next_action") is None

    # Recoverable validation failures are not public process errors.
    assert emit_error.await_count == 1
    assert emit_error.await_args.args[0]["error_code"] == (
        "ACTION_TOOL_ARGS_INVALID_RETRY_EXCEEDED"
    )

    # All failed attempts remain available in internal step history.
    repo = agent.repository  # type: ignore[assignment]
    action_steps = getattr(repo, "data", {}).get("action_steps", [])
    tool_steps = [
        s
        for s in action_steps
        if s.get("action_id") == request.action_id
        and str(s.get("step_name") or "").startswith("tool::memory_search")
    ]
    assert len(tool_steps) == 5

    # ToolValidationError の自己修復ループでも、短縮ID/local_step_number は必ず保存され不変であること
    short_step_ids = [s.get("short_step_id") for s in tool_steps]
    local_step_numbers = [s.get("local_step_number") for s in tool_steps]
    assert all(isinstance(v, str) and v for v in short_step_ids)
    assert all(isinstance(v, int) and v > 0 for v in local_step_numbers)
    assert all(str(v).endswith("-TOOL") for v in short_step_ids)
    assert len(set(short_step_ids)) == 1, f"短縮IDが不変でない: {short_step_ids}"
    assert len(set(local_step_numbers)) == 1, (
        f"local_step_number が不変でない: {local_step_numbers}"
    )

"""Supervisor の必須引数 `step_note` の契約テスト。"""

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
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.executing import (
    EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import (
    STEP_NOTE_ARG,
    STEP_NOTE_MAX_LENGTH,
    build_native_action_tools,
    select_supervisor_act_tool_registry,
)
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.utils.prompt_loader import PromptConfig

_NOTE = "作業計画がまだ無いので、read_action_plan で現状の plan.md を確認する。"


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _runtime_services(agent: ActionAgent):
    return agent._runtime_services  # noqa: SLF001


def _make_agent() -> ActionAgent:
    repo = MockActionAgentRepository()

    def _fake_load_config(_prompt_name: str) -> PromptConfig:
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


async def _build_supervisor_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    action_id: str,
    allowed_tool_ids: tuple[str, ...] = ("memory_search",),
):
    agent = _make_agent()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id=action_id,
        suggestion_id=f"sug-{action_id}",
        user_id=f"user-{action_id}",
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
        emit_error=AsyncMock(),
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
    install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=allowed_tool_ids,
    )
    await agent.repository.save_action(
        {
            "action_id": request.action_id,
            "suggestion_id": request.suggestion_id,
            "user_id": request.user_id,
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
    return agent, runtime, state, request


def test_every_supervisor_tool_requires_a_step_note() -> None:
    native_tools = build_native_action_tools(select_supervisor_act_tool_registry())

    assert native_tools
    for tool in native_tools:
        properties = tool.parameters["properties"]
        assert isinstance(properties, dict)
        assert properties[STEP_NOTE_ARG]["maxLength"] == STEP_NOTE_MAX_LENGTH  # type: ignore[index]
        assert STEP_NOTE_ARG in tool.parameters["required"]  # type: ignore[operator]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_note", "explicit_arguments"),
    [
        (None, {}),
        ("   ", {}),
        ("a" * (STEP_NOTE_MAX_LENGTH + 1), {}),
        (None, {STEP_NOTE_ARG: 12}),
    ],
    ids=["missing", "blank", "too_long", "not_a_string"],
)
async def test_invalid_step_note_never_reaches_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    step_note: str | None,
    explicit_arguments: dict[str, object],
) -> None:
    agent, runtime, state, request = await _build_supervisor_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-step-note-invalid",
    )
    turn = native_tool_turn(
        "memory_search",
        {"query": "past incidents", **explicit_arguments},  # type: ignore[dict-item]
        step_note=step_note,
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        side_effect=[turn] * EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS
    )

    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    state = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    assert agent._generate_llm_action_turn.await_count == (  # type: ignore[attr-defined]
        EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS
    )
    assert state["next_action"] is None
    assert state["status"] == "error"
    think_error = state["errors"][0]
    assert think_error["error_code"] == "ACTION_EXECUTION_THINK_OUTPUT_INVALID"
    assert all(
        STEP_NOTE_ARG in reason
        for reason in think_error["error_details"]["errors"]  # type: ignore[index]
    )

    recorded = agent.repository.data["action_steps"]  # type: ignore[attr-defined]
    assert not [
        step
        for step in recorded
        if step.get("action_id") == request.action_id
        and str(step.get("step_name") or "").startswith("tool::")
    ]


@pytest.mark.asyncio
async def test_step_note_is_stripped_from_args_and_stored_on_both_history_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    agent, runtime, state, request = await _build_supervisor_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-step-note-ok",
        allowed_tool_ids=("read_action_plan",),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=native_tool_turn(
            "read_action_plan",
            {},
            step_note=f"  {_NOTE}  ",
        )
    )

    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    next_action = state["next_action"]
    assert next_action is not None and next_action.tool is not None
    assert next_action.tool.args == {}

    state = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    history = state["history_by_scope"]["S"]
    think_entry = next(
        entry for entry in history if entry["step_type"] == StepType.LLM_OUTPUT
    )
    tool_entry = next(
        entry for entry in history if entry["step_type"] == StepType.TOOL_EXECUTION
    )
    assert think_entry["summary"] == _NOTE
    assert tool_entry["summary"] == _NOTE
    assert think_entry["args"] == {}
    assert tool_entry["args"] == {}
    assert tool_entry["result_line"] == "read_action_plan: ok"

    tool_steps = [
        step
        for step in agent.repository.data["action_steps"]  # type: ignore[attr-defined]
        if step.get("action_id") == request.action_id
        and str(step.get("step_name") or "").startswith("tool::")
    ]
    assert tool_steps
    assert STEP_NOTE_ARG not in tool_steps[0]["tool_args"]["args"]


@pytest.mark.asyncio
async def test_failed_tool_keeps_the_note_and_records_a_result_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    agent, runtime, state, _request = await _build_supervisor_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-step-note-failure",
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=native_tool_turn(
            "memory_search",
            {"query": ""},
            step_note=_NOTE,
        )
    )

    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    state = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    tool_entry = next(
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.TOOL_EXECUTION
    )
    assert tool_entry["summary"] == _NOTE
    assert tool_entry["result_line"] == (
        "memory_search: failed ACTION_TOOL_ARGS_INVALID"
    )

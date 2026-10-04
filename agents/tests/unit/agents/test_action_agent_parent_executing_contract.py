from __future__ import annotations

# pylint: disable=import-error,protected-access
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
from pantaray_agents.agents.action_agent.runtime.state import (
    build_next_action,
    build_tool_call,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.tools import (
    SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_REGISTRY,
    build_native_action_tools,
    select_tool_registry,
)
from pantaray_agents.agents.core import LlmUsage, TokenBudgetExceeded, TokenSink
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.errors import LlmProxyExecutionError

_ROLE_RULES = {
    "supervisor": (
        "- Use subagents only when independent delegation adds clear value."
    ),
    "subagent": "- Report with `submit_subagent_report`.",
}


def _runtime_services(agent: ActionAgent):
    return agent._runtime_services  # noqa: SLF001


def _make_agent() -> ActionAgent:
    """テスト用に最小プロンプトで ActionAgent を構築する。"""

    repo = MockActionAgentRepository()

    def _fake_load_config(prompt_name: str) -> PromptConfig:
        if prompt_name == "action/executing":
            return PromptConfig(
                prompt="{current_time}",
                system_instruction="SYS {role_rules}",
                role_rules=_ROLE_RULES,
            )
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        return ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}}, repository=repo
        )


def test_route_from_think_runs_canonical_action() -> None:
    """executing THINK の決定は canonical ACTION へ渡す。"""

    agent = _make_agent()
    state = {
        "status": "processing",
        "phase": "executing",
        "final_output": None,
        "next_action": build_next_action(
            tool=build_tool_call(tool_id="thinking", args={}),
            decided_at="2026-08-09T00:00:00Z",
        ),
        "context": {
            "use_goal_workers": False,
            "goals": [
                {
                    "id": "G1",
                    "check_item_id": "check_001",
                    "title": "Test goal",
                    "status": "open",
                    "is_active": True,
                }
            ],
        },
    }
    runtime = ActionGraphRuntime(
        agent=agent,
        request=build_action_request(
            action_id="act-route",
            suggestion_id="sug-route",
            user_id="user-route",
        ),
        state_config={
            "max_steps": 10,
            "max_tool_steps": 10,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(agent),
    )

    assert runtime.route_from_think(state) == "action"


def test_native_execution_tools_match_the_parent_tool_surface() -> None:
    """execution provider tool定義は親 ACT の tool surface と一致する。"""

    execution_tools = build_native_action_tools(
        select_tool_registry(SUPERVISOR_SINGLE_REACT_TOOL_IDS)
    )

    assert {tool.name for tool in execution_tools} == set(
        SUPERVISOR_SINGLE_REACT_TOOL_IDS
    )


def test_supervisor_act_tool_ids_are_parent_executable_only() -> None:
    """親 ACT runtime は executing で実行可能な tool だけを受け付ける。"""

    assert set(SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS).issubset(TOOL_REGISTRY)
    assert "bash" in SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS
    assert "initialize_plan" not in SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS
    assert "run_next_goal" not in SUPERVISOR_SINGLE_REACT_ACT_TOOL_IDS


@pytest.mark.asyncio
async def test_execution_think_renders_the_supervisor_role_rules() -> None:
    """親ランタイムでは Supervisor の役割の節だけを system prompt に差し込む。"""

    agent = _make_agent()
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]

    captured_system_instructions: list[str] = []

    async def _fake_generate_llm_action_turn(
        *, prompt: str, system_instruction: str, **_: object
    ):
        _ = prompt
        captured_system_instructions.append(system_instruction)
        return native_tool_turn("thinking", {})

    agent._generate_llm_action_turn = AsyncMock(
        side_effect=_fake_generate_llm_action_turn
    )  # type: ignore[attr-defined]

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id="act-meta-system-rules",
        suggestion_id="sug-meta-system-rules",
        user_id="user-meta-system-rules",
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
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False

    await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )

    assert captured_system_instructions
    system_instruction = captured_system_instructions[0]
    assert "subagents only when independent delegation" in system_instruction
    assert "submit_subagent_report" not in system_instruction
    assert "{role_rules}" not in system_instruction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_budget", "expected_llm_calls", "expected_status"),
    ((None, 2, "processing"), (9, 1, "error")),
)
async def test_execution_think_accounts_repair_usage_and_stops_at_budget(
    token_budget: int | None,
    expected_llm_calls: int,
    expected_status: str,
) -> None:
    agent = _make_agent()
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]
    agent._consume_llm_thoughts = MagicMock(return_value=None)  # type: ignore[attr-defined]
    repair_error = LlmProxyExecutionError(
        error_code="PROXY_LLM_TOOL_CALL_INVALID",
        error_message="multiple calls",
        retryable=False,
        recovery="repair_next_turn",
        usage_metadata={
            "prompt_token_count": 7,
            "completion_token_count": 3,
        },
        tool_call_violation_reason="multiple_calls",
        actual_tool_call_count=2,
    )
    client = _install_usage_recording_calls(
        agent,
        responses=[repair_error, native_tool_turn("thinking", {"query": "x"})],
        usages=[LlmUsage(7, 3), LlmUsage(11, 4)],
    )
    emit_error = AsyncMock()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id="act-meta-repair-usage",
        suggestion_id="sug-meta-repair-usage",
        user_id="user-meta-repair-usage",
    )
    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 10,
            "token_budget": token_budget,
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
        max_steps=10,
        max_tool_steps=10,
        token_budget=token_budget,
    )
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False

    sink = create_state_token_sink(state)
    if token_budget is None:
        updated = await execution_think_step(agent, state, runtime, sink=sink)
    else:
        with pytest.raises(TokenBudgetExceeded):
            await execution_think_step(agent, state, runtime, sink=sink)
        updated = state

    assert client.await_count == expected_llm_calls
    assert updated["status"] == expected_status
    if token_budget is None:
        assert updated["total_prompt_tokens"] == 18
        assert updated["total_completion_tokens"] == 7
        assert updated["tokens_used"] == 25
        # repair 再試行はトークンのみ加算し、論理 LLM ステップは消費しない。
        assert updated["llm_steps_taken"] == 1
        assert updated["next_action"].tool.tool_id == "thinking"
    else:
        assert updated["tokens_used"] == 10
        assert updated["errors"][-1]["error_code"] == ("ACTION_TOKEN_BUDGET_EXCEEDED")


@pytest.mark.asyncio
async def test_execution_think_rejects_initialize_plan() -> None:
    """executing では initialize_plan を repair 対象として扱う。"""

    agent = _make_agent()
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]

    prompts: list[str] = []

    async def _fake_generate_llm_action_turn(
        *, prompt: str, system_instruction: str, **_: object
    ):
        _ = system_instruction
        prompts.append(prompt)
        return (
            native_tool_turn("initialize_plan", {})
            if len(prompts) == 1
            else native_tool_turn("thinking", {})
        )

    agent._generate_llm_action_turn = AsyncMock(
        side_effect=_fake_generate_llm_action_turn
    )  # type: ignore[attr-defined]

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id="act-meta-initialize-plan-retry",
        suggestion_id="sug-meta-initialize-plan-retry",
        user_id="user-meta-initialize-plan-retry",
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
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False

    updated = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )

    assert agent._generate_llm_action_turn.await_count == 2  # type: ignore[attr-defined]
    assert updated["next_action"] and updated["next_action"]["tool"]
    assert updated["next_action"]["tool"]["tool_id"] == "thinking"
    assert len(prompts) == 2
    assert "not listed in Available Tools for the current step" in prompts[1]
    # repair prompt は現在モードで実行可能な tool_id を明示列挙する。
    assert (
        "Allowed tool_id values in this mode: "
        + ", ".join(sorted(SUPERVISOR_SINGLE_REACT_TOOL_IDS))
        in prompts[1]
    )


@pytest.mark.asyncio
async def test_action_step_rejects_tools_outside_the_parent_act_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """親 ACTION は executing で実行可能な tool 以外を実行しない。"""

    agent = _make_agent()
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )

    emit_error = AsyncMock()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id="act-supervisor-act-boundary",
        suggestion_id="sug-supervisor-act-boundary",
        user_id="user-supervisor-act-boundary",
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
        emit_error=emit_error,
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
    state["next_action"] = build_next_action(
        tool=build_tool_call(tool_id="run_next_goal", args={}),
        decided_at=datetime.now(UTC).isoformat(),
    )

    updated = await action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )

    assert updated["next_action"] is None
    assert updated["context"]["tool_validation_error_streak"] == 1
    assert updated.get("errors")
    latest_error = updated["errors"][-1]
    assert latest_error["error_code"] == "ACTION_TOOL_CALL_INVALID"
    assert latest_error["error_details"]["tool_id"] == "run_next_goal"
    allowed_tool_ids = latest_error["error_details"]["validation"]["metadata"][
        "allowed_tool_ids"
    ]
    assert "run_next_goal" not in allowed_tool_ids
    assert "initialize_plan" not in allowed_tool_ids


@pytest.mark.asyncio
async def test_execution_think_stops_with_error_after_five_invalid_outputs() -> None:
    """execution THINK が5回連続で不正出力なら理由を残して停止する。"""

    agent = _make_agent()
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent.repository.save_action_step = AsyncMock()  # type: ignore[attr-defined]

    invalid_turn = native_tool_turn("initialize_plan", {})
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        side_effect=[invalid_turn] * 5
    )

    emit_error = AsyncMock()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id="act-meta-stop",
        suggestion_id="sug-meta-stop",
        user_id="user-meta-stop",
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
        emit_error=emit_error,
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
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    state["context"]["use_goal_workers"] = False

    updated = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )

    assert agent._generate_llm_action_turn.await_count == 5  # type: ignore[attr-defined]
    assert updated["status"] == "error"
    assert updated.get("final_output") in ("", None)
    assert updated.get("next_action") is None
    assert updated.get("errors"), "errors が記録されていません"
    assert any(
        err.get("error_code") == "ACTION_EXECUTION_THINK_OUTPUT_INVALID"
        for err in updated["errors"]
        if isinstance(err, dict)
    )
    emit_error.assert_awaited()


def _install_usage_recording_calls(
    agent: ActionAgent,
    *,
    responses: list[object],
    usages: list[LlmUsage],
) -> AsyncMock:
    response_iterator = iter(responses)
    usage_iterator = iter(usages)
    client = AsyncMock()

    async def generate(
        *_args: object,
        sink: TokenSink,
        stage: str | None = None,
        **_kwargs: object,
    ) -> object:
        sink.guard()
        await client()
        response = next(response_iterator)
        usage = next(usage_iterator)
        sink.record(
            usage,
            stage=stage,
            may_raise=not isinstance(response, BaseException),
        )
        if isinstance(response, BaseException):
            raise response
        return response

    agent._generate_llm_action_turn = generate  # type: ignore[method-assign]
    return client

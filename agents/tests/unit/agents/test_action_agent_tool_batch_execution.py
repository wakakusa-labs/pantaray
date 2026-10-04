"""ACTION ノードによるツールバッチ実行と step 採番の契約テスト。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    project_request_user_step,
)

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act import (
    call_execution,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act import step as act
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    CompletedToolControl,
    ToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.tools import (
    STEP_NOTE_ARG,
    select_supervisor_act_tool_registry,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.tool_use import LlmToolCall

PLAN_TOOL = "read_action_plan"
PATCH_TOOL = "apply_patch"
_PATCH_ARGS: dict[str, JSONValue] = {
    "changes": [
        {
            "kind": "update_file",
            "path": "notes.txt",
            "unified_diff": "@@ -1 +1 @@\n-a\n+b\n",
        }
    ]
}


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _note(index: int) -> str:
    return f"{index} 件目の呼び出しとして、必要な情報をここで確認する。"


def _turn(*calls: tuple[str, dict[str, JSONValue]]) -> LlmActionTurnResponse:
    return LlmActionTurnResponse(
        mode="action_turn",
        messages=[],
        calls=[
            LlmToolCall(
                call_id=f"call-{index}",
                name=tool_id,
                arguments={**args, STEP_NOTE_ARG: _note(index)},
            )
            for index, (tool_id, args) in enumerate(calls)
        ],
    )


def _make_agent() -> ActionAgent:
    def _fake_load_config(_prompt_name: str) -> PromptConfig:
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=MockActionAgentRepository(),
        )
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent._consume_llm_thoughts = MagicMock(return_value=None)  # type: ignore[attr-defined]
    return agent


async def _build_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    action_id: str,
    allowed_tool_ids: tuple[str, ...],
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
        services=agent._runtime_services,  # noqa: SLF001
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


async def _think(agent, runtime, state: ActionAgentState) -> ActionAgentState:
    return await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )


async def _act(agent, runtime, state: ActionAgentState) -> ActionAgentState:
    return await act.action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )


def _tool_history(state: ActionAgentState) -> list[dict[str, Any]]:
    return [
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.TOOL_EXECUTION
    ]


def _think_history(state: ActionAgentState) -> dict[str, Any]:
    return next(
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.LLM_OUTPUT
    )


def _persisted_tool_steps(agent, action_id: str) -> list[dict[str, Any]]:
    return sorted(
        (
            step
            for step in agent.repository.data["action_steps"]
            if step.get("action_id") == action_id
            and str(step.get("step_name") or "").startswith("tool::")
        ),
        key=lambda step: step["step_number"],
    )


def _success_result(*, step_id: str, tool_id: str) -> ToolExecutionResult:
    now = datetime.now(UTC).isoformat()
    return ToolExecutionResult(
        step_id=step_id,
        tool_id=tool_id,
        status="success",
        started_at=now,
        completed_at=now,
        finalized_output=FinalizedToolOutput(
            output={"tool_id": tool_id},
            storage_kind="inline_json",
            owner_kind="tool_invocation",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
        ),
        control=CompletedToolControl(),
    )


@pytest.mark.asyncio
async def test_parallel_batch_numbers_and_parents_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """K=3 の並列バッチは 3 行の TOOL step を連番・別 short id で残す。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-batch-parallel",
        allowed_tool_ids=(PLAN_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn((PLAN_TOOL, {}), (PLAN_TOOL, {}), (PLAN_TOOL, {}))
    )

    state = await _think(agent, runtime, state)
    batch = state["next_action"].batch
    assert batch is not None
    assert batch.mode == "parallel"
    base_step_number = state["step"]

    state = await _act(agent, runtime, state)

    tool_entries = _tool_history(state)
    assert [entry["step_number"] for entry in tool_entries] == [
        base_step_number,
        base_step_number + 1,
        base_step_number + 2,
    ]
    assert [entry["summary"] for entry in tool_entries] == [
        _note(0),
        _note(1),
        _note(2),
    ]
    assert {entry["result_line"] for entry in tool_entries} == {
        f"{PLAN_TOOL}: ok",
    }

    think_entry = _think_history(state)
    think_local = int(str(think_entry["short_step_id"]).split("-")[1])
    assert [entry["short_step_id"] for entry in tool_entries] == [
        f"S-{think_local}-TOOL",
        f"S-{think_local + 1}-TOOL",
        f"S-{think_local + 2}-TOOL",
    ]

    persisted = _persisted_tool_steps(agent, request.action_id)
    assert len(persisted) == 3
    assert {step["parent_step_id"] for step in persisted} == {think_entry["step_id"]}
    assert [step["step_number"] for step in persisted] == [
        base_step_number,
        base_step_number + 1,
        base_step_number + 2,
    ]
    assert len({step["step_id"] for step in persisted}) == 3


@pytest.mark.asyncio
async def test_batch_consumes_one_tool_step_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """バッチ K 件は logical step も tool step 予算も K 消費する。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-batch-budget",
        allowed_tool_ids=(PLAN_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn((PLAN_TOOL, {}), (PLAN_TOOL, {}), (PLAN_TOOL, {}))
    )

    state = await _think(agent, runtime, state)
    base_step_number = state["step"]
    base_tool_steps = state["tool_steps_taken"]
    llm_steps = state["llm_steps_taken"]

    state = await _act(agent, runtime, state)

    assert state["step"] == base_step_number + 3
    assert state["tool_steps_taken"] == base_tool_steps + 3
    assert state["steps_taken"] == llm_steps + base_tool_steps + 3
    persisted = _persisted_tool_steps(agent, request.action_id)
    assert max(step["step_number"] for step in persisted) == state["step"] - 1


def test_call_timeout_comes_from_the_declared_tool_policy() -> None:
    """per-call timeout は既存の ``default_timeout_ms`` をそのまま使う。"""

    tool_def = select_supervisor_act_tool_registry()[PLAN_TOOL]
    slot = MagicMock()
    slot.tool_def = tool_def
    assert act.call_timeout_seconds(slot) == (
        tool_def.execution_policy.default_timeout_ms / 1000
    )


@pytest.mark.asyncio
async def test_parallel_call_timeout_only_fails_that_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """per-call timeout を超えた呼び出しだけが TOOL_BATCH_TIMEOUT で確定する。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-batch-timeout",
        allowed_tool_ids=(PLAN_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn((PLAN_TOOL, {}), (PLAN_TOOL, {}), (PLAN_TOOL, {}))
    )

    state = await _think(agent, runtime, state)
    slow_step_number = state["step"] + 1

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        if kwargs["step_number"] == slow_step_number:
            await asyncio.sleep(5)
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)
    monkeypatch.setattr(act, "call_timeout_seconds", lambda _slot: 0.05)

    state = await _act(agent, runtime, state)

    tool_entries = _tool_history(state)
    assert len(tool_entries) == 3
    timed_out = [
        entry
        for entry in tool_entries
        if entry["result_line"] == f"{PLAN_TOOL}: failed TOOL_BATCH_TIMEOUT"
    ]
    assert len(timed_out) == 1
    assert timed_out[0]["step_number"] == slow_step_number
    assert [
        entry["result_line"]
        for entry in tool_entries
        if entry["step_number"] != slow_step_number
    ] == [f"{PLAN_TOOL}: ok", f"{PLAN_TOOL}: ok"]

    persisted = _persisted_tool_steps(agent, request.action_id)
    assert len(persisted) == 3
    assert [step["status"] for step in persisted] == ["success", "error", "success"]
    assert state["errors"][-1]["error_code"] == "TOOL_BATCH_TIMEOUT"


@pytest.mark.asyncio
async def test_sequential_batch_never_overlaps_workspace_lock_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``apply_patch`` ×2 は逐次で走り、workspace root lock を奪い合わない。

    ``PATCH_LOCK_CONFLICT`` は 2 つ目の ``apply_patch`` が 1 つ目のロック保持中に
    実行を始めたときだけ起きる。ここでは ACTION ノードが所有する条件、つまり
    「宣言順に 1 件ずつ、重ならずに実行する」ことを検証する。
    """

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-batch-sequential",
        allowed_tool_ids=(PATCH_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn((PATCH_TOOL, _PATCH_ARGS), (PATCH_TOOL, _PATCH_ARGS))
    )

    state = await _think(agent, runtime, state)
    assert state["next_action"].batch.mode == "sequential"

    in_flight = 0
    max_in_flight = 0

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)

    state = await _act(agent, runtime, state)

    assert max_in_flight == 1
    assert len(_persisted_tool_steps(agent, request.action_id)) == 2
    assert [entry["step_number"] for entry in _tool_history(state)] == [
        state["step"] - 2,
        state["step"] - 1,
    ]

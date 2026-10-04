"""バッチ途中で落ちた run の checkpoint 往復と残バッチ再開の契約テスト。"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    project_request_user_step,
)

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
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
from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.application.action.resume_service import (
    _RESTORED_OPTIONAL_STATE_FIELDS,
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

PATCH_TOOL = "apply_patch"
PLAN_TOOL = "read_action_plan"
_PATCH_ARGS: dict[str, JSONValue] = {
    "changes": [
        {
            "kind": "update_file",
            "path": "notes.txt",
            "unified_diff": "@@ -1 +1 @@\n-a\n+b\n",
        }
    ]
}


class _SimulatedProcessCrash(BaseException):
    """ツール実行中のプロセス消失を模す（``Exception`` 経路では捕まらない）。"""


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _note(index: int) -> str:
    return f"{index} 件目の呼び出しとして、必要な情報をここで確認する。"


def _turn(count: int) -> LlmActionTurnResponse:
    return LlmActionTurnResponse(
        mode="action_turn",
        messages=[],
        calls=[
            LlmToolCall(
                call_id=f"call-{index}",
                name=PATCH_TOOL,
                arguments={**_PATCH_ARGS, STEP_NOTE_ARG: _note(index)},
            )
            for index in range(count)
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


async def _build_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path, *, action_id: str):
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
        allowed_tool_ids=(PATCH_TOOL, PLAN_TOOL),
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


def _tool_history(state: ActionAgentState) -> list[dict[str, Any]]:
    return [
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.TOOL_EXECUTION
    ]


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


def _restore(checkpoint, request) -> ActionAgentState:
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id=request.action_id,
        expected_suggestion_id=request.suggestion_id,
        expected_user_id=request.user_id,
    )
    # resume_service.apply_latest_runtime_state_config と同じ復元後の正規化。
    mutable = cast("MutableMapping[str, object]", restored)
    for field_name, default_value in _RESTORED_OPTIONAL_STATE_FIELDS:
        mutable.setdefault(field_name, default_value)
    restored["token_budget"] = None
    return restored


@pytest.mark.asyncio
async def test_each_persisted_call_checkpoints_only_unexecuted_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """K=3 の各 TOOL step が残す checkpoint は、その時点の残バッチだけを載せる。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-batch-remainder"
    )
    agent._generate_llm_action_turn = AsyncMock(return_value=_turn(3))  # type: ignore[attr-defined]

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)

    state = await _think(agent, runtime, state)
    assert state["next_action"].batch.mode == "sequential"

    state = await _act(agent, runtime, state)

    persisted = _persisted_tool_steps(agent, request.action_id)
    remaining_notes = [
        [
            call["step_note"]
            for call in (step["runtime_state_checkpoint"]["next_action"] or {}).get(
                "batch", {"calls": []}
            )["calls"]
        ]
        if step["runtime_state_checkpoint"].get("next_action") is not None
        else []
        for step in persisted
    ]
    assert remaining_notes == [[_note(1), _note(2)], [_note(2)], []]

    restored = _restore(persisted[0]["runtime_state_checkpoint"], request)
    next_action = restored["next_action"]
    assert next_action is not None
    assert next_action.batch is not None
    assert next_action.tool == next_action.batch.calls[0].call
    assert [call.call.args for call in next_action.batch.calls] == [
        _PATCH_ARGS,
        _PATCH_ARGS,
    ]
    assert state["next_action"] is None


@pytest.mark.asyncio
async def test_a_parallel_batch_checkpoints_no_remaining_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """並列バッチは残りを持ち越さない。

    兄弟は宣言順どおりに完了しないのに行の ``step_number`` は宣言順で固定されており、
    crash recovery は ``step_number`` が最大の行を選ぶ。後ろの呼び出しが先に完了した行に
    残りが載っていると、完了済みの前の呼び出しを再実行してその History Ref を奪う。
    """

    agent, runtime, state, request = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-batch-parallel-remainder"
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=LlmActionTurnResponse(
            mode="action_turn",
            messages=[],
            calls=[
                LlmToolCall(
                    call_id=f"call-{index}",
                    name=PLAN_TOOL,
                    arguments={STEP_NOTE_ARG: _note(index)},
                )
                for index in range(3)
            ],
        )
    )

    state = await _think(agent, runtime, state)
    assert state["next_action"].batch.mode == "parallel"
    last_step_number = state["step"] + 2

    async def _finish_in_reverse(_agent, tool_def, _args, _state, **kwargs):
        # 宣言順の逆に完了させ、後ろの呼び出しの行を先に永続化する。
        await asyncio.sleep(0.01 * (last_step_number - kwargs["step_number"]))
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _finish_in_reverse)
    state = await _act(agent, runtime, state)

    persisted = _persisted_tool_steps(agent, request.action_id)
    assert len(persisted) == 3
    assert all(
        step["runtime_state_checkpoint"].get("next_action") is None
        for step in persisted
    )
    assert state["next_action"] is None


@pytest.mark.asyncio
async def test_resume_after_a_crash_runs_only_the_remaining_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """K=3 で 2 件永続後に落ちたら、resume は 1 件だけを新しい行として実行する。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-batch-crash"
    )
    agent._generate_llm_action_turn = AsyncMock(return_value=_turn(3))  # type: ignore[attr-defined]

    state = await _think(agent, runtime, state)
    base_step_number = state["step"]
    crashed_step_number = base_step_number + 2
    pending_origin = state["next_action"].batch.calls[2].origin

    async def _crash_on_third(_agent, tool_def, _args, _state, **kwargs):
        if kwargs["step_number"] == crashed_step_number:
            raise _SimulatedProcessCrash
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _crash_on_third)

    with pytest.raises(_SimulatedProcessCrash):
        await _act(agent, runtime, state)

    persisted = _persisted_tool_steps(agent, request.action_id)
    assert [step["step_number"] for step in persisted] == [
        base_step_number,
        base_step_number + 1,
    ]

    resumed = _restore(persisted[-1]["runtime_state_checkpoint"], request)
    assert resumed["step"] == crashed_step_number
    assert len(resumed["next_action"].batch.calls) == 1
    assert resumed["next_action"].batch.calls[0].step_note == _note(2)
    assert resumed["next_action"].batch.calls[0].origin == pending_origin
    assert pending_origin.call_id == "call-2"

    executed_step_numbers: list[int] = []

    async def _record_run(_agent, tool_def, _args, _state, **kwargs):
        executed_step_numbers.append(kwargs["step_number"])
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _record_run)
    resumed = await _act(agent, runtime, resumed)

    assert executed_step_numbers == [crashed_step_number]
    assert resumed["next_action"] is None
    assert resumed["step"] == crashed_step_number + 1

    tool_entries = _tool_history(resumed)
    assert [entry["step_number"] for entry in tool_entries] == [
        base_step_number,
        base_step_number + 1,
        crashed_step_number,
    ]
    # 完了済みの兄弟が持つ History Ref を再開分が奪わない（upsert で消えてしまう）。
    think_local = int(
        str(
            next(
                entry
                for entry in resumed["history_by_scope"]["S"]
                if entry["step_type"] == StepType.LLM_OUTPUT
            )["short_step_id"]
        ).split("-")[1]
    )
    assert [entry["short_step_id"] for entry in tool_entries] == [
        f"S-{think_local}-TOOL",
        f"S-{think_local + 1}-TOOL",
        f"S-{think_local + 2}-TOOL",
    ]
    durable = _persisted_tool_steps(agent, request.action_id)
    assert max(step["step_number"] for step in durable) == resumed["step"] - 1
    assert len({step["short_step_id"] for step in durable}) == 3


@pytest.mark.asyncio
async def test_legacy_checkpoint_without_a_batch_still_runs_the_single_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``next_action.batch`` を持たない既存 checkpoint は単発経路のまま動く。"""

    agent, runtime, state, request = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-batch-legacy"
    )
    agent._generate_llm_action_turn = AsyncMock(return_value=_turn(1))  # type: ignore[attr-defined]

    state = await _think(agent, runtime, state)
    checkpoint = build_runtime_state_checkpoint(state)
    del checkpoint["next_action"]["batch"]

    restored = _restore(checkpoint, request)
    assert restored["next_action"].batch is None

    executed_step_numbers: list[int] = []

    async def _record_run(_agent, tool_def, _args, _state, **kwargs):
        executed_step_numbers.append(kwargs["step_number"])
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _record_run)
    restored = await _act(agent, runtime, restored)

    assert executed_step_numbers == [restored["step"] - 1]
    assert restored["next_action"] is None
    tool_entries = _tool_history(restored)
    assert len(tool_entries) == 1
    think_local = int(
        str(
            next(
                entry
                for entry in restored["history_by_scope"]["S"]
                if entry["step_type"] == StepType.LLM_OUTPUT
            )["short_step_id"]
        ).split("-")[1]
    )
    assert tool_entries[0]["short_step_id"] == f"S-{think_local}-TOOL"

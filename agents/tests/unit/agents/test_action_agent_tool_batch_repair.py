"""バッチ途中の ToolValidationError から修復するときの採番契約テスト。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.test_action_agent_tool_batch_execution import (
    SQL_CALL,
    SQL_TOOL,
    _act,
    _build_fixture,
    _persisted_tool_steps,
    _success_result,
    _think,
    _tool_history,
    _turn,
)

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act import (
    call_execution,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _think_refs(state: ActionAgentState) -> list[str]:
    return [
        str(entry["short_step_id"])
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.LLM_OUTPUT
    ]


def _by_step_number(state: ActionAgentState) -> dict[int, dict[str, Any]]:
    return {int(entry["step_number"]): entry for entry in _tool_history(state)}


@pytest.mark.asyncio
async def test_repair_after_a_partly_executed_batch_keeps_completed_sibling_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """バッチ内 1 件が ToolValidationError で失敗しても、完了した兄弟の行は残る。

    修復ターンの THINK が直前 THINK の History Ref を再利用すると、再試行のバッチ先頭が
    完了済みの 1 件目と同じ ``short_step_id`` を取り、``upsert_history_entry`` がその行を
    置き換えてしまう。修復ターンには新しい番号を与え、再試行には新しい Ref を配る。
    """

    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-batch-repair",
        allowed_tool_ids=(SQL_TOOL,),
    )
    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn(SQL_CALL, SQL_CALL, SQL_CALL)
    )

    state = await _think(agent, runtime, state)
    base_step_number = state["step"]
    invalid_step_number = base_step_number + 1

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs["step_number"] == invalid_step_number:
            # 兄弟が全件終わったあとに失敗させ、validation streak が残ったまま
            # THINK へ戻る（=修復ターンになる）状況を決定的に作る。
            await asyncio.sleep(0.05)
            raise ToolValidationError("args are invalid", details={"path": ["args"]})
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)

    state = await _act(agent, runtime, state)

    executed = _by_step_number(state)
    assert sorted(executed) == [
        base_step_number,
        base_step_number + 1,
        base_step_number + 2,
    ]
    assert state["context"]["tool_validation_error_streak"] == 1
    completed_head = dict(executed[base_step_number])
    assert completed_head["result_line"] == f"{SQL_TOOL}: ok"

    agent._generate_llm_action_turn = AsyncMock(  # type: ignore[attr-defined]
        return_value=_turn(SQL_CALL, SQL_CALL)
    )
    state = await _think(agent, runtime, state)
    state = await _act(agent, runtime, state)

    repaired = _by_step_number(state)
    assert sorted(repaired) == [base_step_number + offset for offset in range(5)]
    head = repaired[base_step_number]
    assert head["short_step_id"] == completed_head["short_step_id"]
    assert head["step_id"] == completed_head["step_id"]
    assert head["result_line"] == f"{SQL_TOOL}: ok"
    refs = [str(entry["short_step_id"]) for entry in _tool_history(state)]
    assert len(refs) == len(set(refs)) == 5

    think_refs = _think_refs(state)
    assert len(think_refs) == len(set(think_refs)) == 2
    persisted = _persisted_tool_steps(agent, request.action_id)
    assert max(step["step_number"] for step in persisted) == state["step"] - 1
    assert state["step"] == base_step_number + 5

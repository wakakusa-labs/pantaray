"""Supervisor THINK が 1 ターンで決めるツールバッチの契約テスト。"""

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

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.executing import (
    EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS,
)
from pantaray_agents.agents.action_agent.runtime.models import ResumeFailureException
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.config_tunables import load_local_runtime_tunables
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.tool_use import LlmToolCall

MAX_PARALLEL = load_local_runtime_tunables().action_agent.max_parallel_tool_calls


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _call(
    tool_id: str,
    args: dict[str, JSONValue] | None = None,
    *,
    note: str | None = None,
    index: int = 0,
) -> LlmToolCall:
    arguments: dict[str, JSONValue] = dict(args or {})
    if note is not None:
        arguments[STEP_NOTE_ARG] = note
    return LlmToolCall(call_id=f"call-{index}", name=tool_id, arguments=arguments)


def _turn(
    *calls: LlmToolCall,
    dropped_call_names: tuple[str, ...] = (),
) -> LlmActionTurnResponse:
    return LlmActionTurnResponse(
        mode="action_turn",
        messages=[],
        calls=list(calls),
        dropped_call_names=list(dropped_call_names),
    )


def _note_for(tool_id: str) -> str:
    return f"{tool_id} の結果が必要なので、この呼び出しで確認する。"


def _batch_turn(*tool_ids: str) -> LlmActionTurnResponse:
    return _turn(
        *(
            _call(tool_id, note=_note_for(tool_id), index=index)
            for index, tool_id in enumerate(tool_ids)
        )
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


async def _build_supervisor_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    action_id: str,
    max_tool_steps: int = 20,
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
        max_tool_steps=max_tool_steps,
        token_budget=None,
    )
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=("read", "memory_search"),
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
    return agent, runtime, state


async def _think(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    action_id: str,
    turns: LlmActionTurnResponse | list[LlmActionTurnResponse],
    max_tool_steps: int = 20,
) -> tuple[ActionAgent, ActionGraphRuntime, ActionAgentState]:
    agent, runtime, state = await _build_supervisor_fixture(
        monkeypatch,
        tmp_path,
        action_id=action_id,
        max_tool_steps=max_tool_steps,
    )
    if isinstance(turns, list):
        agent._generate_llm_action_turn = AsyncMock(side_effect=turns)  # type: ignore[attr-defined]
    else:
        agent._generate_llm_action_turn = AsyncMock(return_value=turns)  # type: ignore[attr-defined]
    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    return agent, runtime, state


def _think_history_entry(state: ActionAgentState) -> dict[str, object]:
    return next(
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.LLM_OUTPUT
    )


def _require_batch(state: ActionAgentState):
    next_action = state["next_action"]
    assert next_action is not None
    batch = next_action.batch
    assert batch is not None
    # ``tool`` は常に先頭要素の射影である。
    assert next_action.tool == batch.calls[0].call
    return batch


@pytest.mark.asyncio
async def test_read_only_batch_runs_in_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-parallel",
        turns=_batch_turn("read_action_plan", "history_fetch", "memory_sql"),
    )

    batch = _require_batch(state)
    assert batch.mode == "parallel"
    assert [pending.tool_id for pending in batch.calls] == [
        "read_action_plan",
        "history_fetch",
        "memory_sql",
    ]
    assert [pending.step_note for pending in batch.calls] == [
        _note_for("read_action_plan"),
        _note_for("history_fetch"),
        _note_for("memory_sql"),
    ]

    request_kwargs = agent._generate_llm_action_turn.await_args.kwargs  # type: ignore[attr-defined]
    assert request_kwargs["max_parallel_tool_calls"] == MAX_PARALLEL

    think_entry = _think_history_entry(state)
    assert think_entry["summary"] == (
        f"1. read_action_plan: {_note_for('read_action_plan')}\n"
        f"2. history_fetch: {_note_for('history_fetch')}\n"
        f"3. memory_sql: {_note_for('memory_sql')}"
    )
    assert "result_line" not in think_entry or not think_entry["result_line"]


@pytest.mark.asyncio
async def test_batch_containing_a_serial_only_tool_is_sequential(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-sequential",
        turns=_turn(
            _call("read", {"path": "a.md"}, note=_note_for("read"), index=0),
            _call(
                "apply_patch",
                {"patch": "diff"},
                note=_note_for("apply_patch"),
                index=1,
            ),
        ),
    )

    batch = _require_batch(state)
    assert batch.mode == "sequential"
    assert [pending.tool_id for pending in batch.calls] == ["read", "apply_patch"]


@pytest.mark.asyncio
async def test_wait_subagents_is_detached_and_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-wait",
        turns=_batch_turn("read", "wait_subagents"),
    )

    batch = _require_batch(state)
    assert [pending.tool_id for pending in batch.calls] == ["read"]

    notice = _think_history_entry(state)["result_line"]
    assert isinstance(notice, str)
    assert "wait_subagents" in notice
    assert "must be the only call of its turn" in notice
    # 次ターンのプロンプトに載る履歴本体へ届いていること。
    assert notice in runtime.services.rendering.format_history(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ("first", "last"))
async def test_a_final_answer_mixed_with_other_calls_waits_for_its_own_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path, position: str
) -> None:
    """最終回答を他の呼び出しと同じターンで出すと、回答は実行せず他だけを実行する。"""

    tool_ids = (
        ("submit_final_answer", "read", "memory_sql")
        if position == "first"
        else ("read", "memory_sql", "submit_final_answer")
    )
    _agent, runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id=f"act-batch-final-{position}",
        turns=_batch_turn(*tool_ids),
    )

    # The Action cannot end on this turn, and the other calls are what runs.
    batch = _require_batch(state)
    assert [pending.tool_id for pending in batch.calls] == ["read", "memory_sql"]
    notice = _think_history_entry(state)["result_line"]
    assert isinstance(notice, str)
    assert "submit_final_answer (must be the only call of its turn" in notice
    assert notice in runtime.services.rendering.format_history(state)

    # Asked again on a turn of its own, it is the call that runs.
    _agent, _runtime, alone = await _think(
        monkeypatch,
        tmp_path / "alone",
        action_id=f"act-batch-final-alone-{position}",
        turns=_batch_turn("submit_final_answer"),
    )
    assert [pending.tool_id for pending in _require_batch(alone).calls] == [
        "submit_final_answer"
    ]


@pytest.mark.asyncio
async def test_two_final_answers_in_one_turn_run_neither(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """最終回答を 2 件出したターンは、どちらも確定させず 1 件だけを出し直させる。"""

    _agent, runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-two-final",
        turns=_turn(
            _call(
                "submit_final_answer", note=_note_for("submit_final_answer"), index=0
            ),
            _call(
                "submit_final_answer", note="訂正したので、こちらで確定する。", index=1
            ),
        ),
    )

    # Nothing runs, so neither answer ends the Action, and the run goes on.
    assert state["next_action"] is None
    assert state["status"] == "processing"
    notice = _think_history_entry(state)["result_line"]
    assert isinstance(notice, str)
    assert notice.count("submit_final_answer (must be the only call of its turn") == 2
    assert "send exactly one, alone" in notice
    assert notice in runtime.services.rendering.format_history(state)


@pytest.mark.asyncio
async def test_calls_above_the_limit_are_trimmed_in_declared_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-trim",
        turns=_batch_turn("read", "list", "glob", "grep", "web_search"),
    )

    batch = _require_batch(state)
    assert [pending.tool_id for pending in batch.calls] == ["read", "list", "glob"]
    assert len(batch.calls) == MAX_PARALLEL

    notice = _think_history_entry(state)["result_line"]
    assert isinstance(notice, str)
    assert notice.startswith(f"Ran {MAX_PARALLEL} of 5 requested tool calls.")
    assert "grep (exceeded the parallel tool call limit of this turn)" in notice
    assert "web_search (exceeded the parallel tool call limit of this turn)" in notice


@pytest.mark.asyncio
async def test_remaining_tool_step_budget_trims_the_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-budget",
        turns=_batch_turn("read", "list", "glob"),
        max_tool_steps=2,
    )

    batch = _require_batch(state)
    assert [pending.tool_id for pending in batch.calls] == ["read", "list"]
    notice = _think_history_entry(state)["result_line"]
    assert isinstance(notice, str)
    assert "glob (exceeded the remaining tool step budget)" in notice


@pytest.mark.asyncio
async def test_provider_dropped_calls_are_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-provider-drop",
        turns=_turn(
            _call("read", {"path": "a.md"}, note=_note_for("read")),
            dropped_call_names=("web_search",),
        ),
    )

    notice = _think_history_entry(state)["result_line"]
    assert isinstance(notice, str)
    assert "web_search (was dropped by the model provider" in notice


@pytest.mark.asyncio
async def test_memory_epoch_writers_are_executed_sequentially(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-memory-epoch",
        turns=_batch_turn("memory_search", "get_memory_reference"),
    )

    batch = _require_batch(state)
    # どちらも state["memory_context_epoch"] を read-modify-write するため並列化しない。
    assert batch.mode == "sequential"
    assert [pending.tool_id for pending in batch.calls] == [
        "memory_search",
        "get_memory_reference",
    ]


@pytest.mark.asyncio
async def test_missing_step_note_names_the_offending_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    turn = _turn(
        _call("read", {"path": "a.md"}, note=_note_for("read"), index=0),
        _call("memory_sql", {"sql": "SELECT 1"}, index=1),
    )
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-note-missing",
        turns=[turn] * EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS,
    )

    assert state["next_action"] is None
    assert state["status"] == "error"
    error = state["errors"][0]
    assert error["error_code"] == "ACTION_EXECUTION_THINK_OUTPUT_INVALID"
    # The failed THINK row occupies step 2, so a later USER turn must start at 3.
    assert _think_history_entry(state)["step_number"] == 2
    assert state["step"] == 3
    reasons = error["error_details"]["errors"]  # type: ignore[index]
    assert all(
        reason.startswith("call #2 (tool_id 'memory_sql')") for reason in reasons
    )
    assert all(STEP_NOTE_ARG in reason for reason in reasons)


@pytest.mark.asyncio
async def test_disallowed_tool_names_the_offending_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    turn = _turn(
        _call("read", {"path": "a.md"}, note=_note_for("read"), index=0),
        _call("not_a_tool", {}, note="使えないツールを呼ぶ。", index=1),
    )
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-tool-unknown",
        turns=[turn] * EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS,
    )

    assert state["next_action"] is None
    reasons = state["errors"][0]["error_details"]["errors"]  # type: ignore[index]
    assert all(
        reason.startswith("call #2 (tool_id 'not_a_tool') is not listed")
        for reason in reasons
    )


@pytest.mark.asyncio
async def test_single_call_turn_keeps_the_existing_think_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-single",
        turns=_turn(_call("read", {"path": "a.md"}, note=_note_for("read"))),
    )

    batch = _require_batch(state)
    assert batch.mode == "sequential"
    assert len(batch.calls) == 1

    think_entry = _think_history_entry(state)
    assert think_entry["summary"] == _note_for("read")
    assert think_entry["tool_id"] == "read"
    assert think_entry["args"] == {"path": "a.md"}
    assert think_entry.get("result_line") is None


@pytest.mark.asyncio
async def test_pending_batch_round_trips_through_the_runtime_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-checkpoint",
        turns=_batch_turn("read", "list"),
    )

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id=state["action_id"],
        expected_suggestion_id=state["suggestion_id"],
        expected_user_id=state["user_id"],
    )

    restored_next_action = restored["next_action"]
    assert restored_next_action == state["next_action"]
    restored_batch = restored_next_action.batch  # type: ignore[union-attr]
    assert restored_batch is not None
    assert [pending.tool_id for pending in restored_batch.calls] == ["read", "list"]
    assert [pending.origin.call_id for pending in restored_batch.calls] == [
        "call-0",
        "call-1",
    ]
    assert all(
        pending.origin.llm_step_id == _think_history_entry(state)["step_id"]
        for pending in restored_batch.calls
    )
    assert restored_batch.mode == "parallel"
    # K=1 射影は checkpoint 越しでも保たれる。
    assert restored_next_action.tool == restored_batch.calls[0].call  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_duplicate_call_identity_rejects_the_whole_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    turn = _turn(
        _call("read", {"path": "a.md"}, note=_note_for("read")),
        _call("read", {"path": "b.md"}, note=_note_for("read")),
    )
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-duplicate-call-id",
        turns=[turn] * EXECUTION_OUTPUT_REPAIR_MAX_ATTEMPTS,
    )

    assert state["next_action"] is None
    assert state["status"] == "error"
    error = state["errors"][0]
    assert error["error_code"] == "ACTION_EXECUTION_THINK_OUTPUT_INVALID"
    assert all(
        "call_id must be unique" in reason
        for reason in error["error_details"]["errors"]
    )


@pytest.mark.asyncio
async def test_resume_rejects_pending_calls_without_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _agent, _runtime, state = await _think(
        monkeypatch,
        tmp_path,
        action_id="act-batch-missing-origin",
        turns=_batch_turn("read"),
    )
    checkpoint = build_runtime_state_checkpoint(state)
    del checkpoint["next_action"]["batch"]["calls"][0]["origin"]

    with pytest.raises(ResumeFailureException) as caught:
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id=state["action_id"],
            expected_suggestion_id=state["suggestion_id"],
            expected_user_id=state["user_id"],
        )
    assert caught.value.failure_code == "ACTION_RESUME_CHECKPOINT_INVALID"

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import common
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.assistant_message import (
    prepare_llm_turn_commit,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.helpers import (
    _apply_llm_step_state_update,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.recorder import (
    record_llm_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.models.tool_call import (
    PendingToolBatchModel,
    PendingToolCallModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    build_next_action,
    build_tool_call,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.runtime.steps.llm import (
    LLMStepPersistenceError,
)
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.local_runtime.agent_state.action_repository import (
    LocalActionRepository,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_assistant_message import ActionLlmTurnCommit
from pantaray_llm.contracts.action_turn import LlmCommentary

from .test_action_subagent_spawn import TIMESTAMP, _connect, _runtime

LLM_STEP_ID = "13349d96-835c-447e-a9cd-d7bba065186f"


def _state() -> ActionAgentState:
    state = create_initial_state(
        user_id="user-1",
        action_id="action-1",
        suggestion_id=None,
        started_at=TIMESTAMP,
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state = project_persisted_user_request_step(
        state,
        step_id="root-user-step",
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="Start work",
        occurred_at=TIMESTAMP,
        history_phase="init",
    )
    state["phase"] = "executing"
    return state


def _prepare(
    state: ActionAgentState, *, has_messages: bool, has_calls: bool
) -> ActionLlmTurnCommit:
    turn = prepare_llm_turn_commit(
        state,
        llm_step_id=LLM_STEP_ID,
        messages=(
            [
                LlmCommentary(
                    phase="commentary", source_message_id=f"msg-{index}", text=text
                )
                for index, text in enumerate(("確認します。", "関連資料も調べます。"))
            ]
            if has_messages
            else []
        ),
        occurred_at=TIMESTAMP,
    )
    if has_calls:
        calls = tuple(
            PendingToolCallModel(
                call=build_tool_call(
                    tool_id="bash", args={"command": command, "cwd": "."}
                ),
                step_note="資料の内容を確認する。",
                origin=ActionToolCallOrigin(
                    llm_step_id=LLM_STEP_ID, call_id=f"call-{index}"
                ),
            )
            for index, command in enumerate(("pwd", "ls"))
        )
        state["next_action"] = build_next_action(
            tool=calls[0].call,
            batch=PendingToolBatchModel(calls=calls, mode="sequential"),
            decided_at=TIMESTAMP,
        )
    _apply_llm_step_state_update(state, decided_at=TIMESTAMP)
    common.get_or_increment_local_step_number(state, "S")
    return turn


async def _record(
    db_path: Path, state: ActionAgentState, turn: ActionLlmTurnCommit
) -> None:
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    agent = cast(ActionAgent, SimpleNamespace(repository=repo))
    local_number = state["context"]["local_step_counters"]["S"]
    await record_llm_step(
        agent,
        state,
        scope_handle="S",
        step_id=LLM_STEP_ID,
        step_number=state["step"],
        step_name="supervisor_think",
        phase="executing",
        summary="",
        llm_prompt_text="Synthetic fixture input",
        llm_response_text="Synthetic accepted response",
        status="success",
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        short_step_id=f"S-{local_number}-THINK",
        local_step_number=local_number,
        infer_parent_from_scope_history=True,
        llm_turn=turn,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "has_messages,has_calls", [(True, True), (True, False), (False, True)]
)
async def test_recording_persists_ordered_messages_and_pending_calls_once(
    tmp_path: Path, has_messages: bool, has_calls: bool
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    initial = _state()
    state = copy.deepcopy(initial)
    turn = _prepare(state, has_messages=has_messages, has_calls=has_calls)
    await _record(db_path, state, turn)
    retry = copy.deepcopy(initial)
    retry_turn = _prepare(retry, has_messages=has_messages, has_calls=has_calls)
    await _record(db_path, retry, retry_turn)

    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM agent_action_steps ORDER BY step_number"
        ).fetchall()
    message_count = 2 if has_messages else 0
    assert len(rows) == 2 + message_count
    assert [row["step_id"] for row in rows[1:-1]] == [
        message.step_id for message in turn.messages
    ]
    assert rows[-1]["parent_step_id"] == "root-user-step"
    assert all(row["parent_step_id"] == LLM_STEP_ID for row in rows[1:-1])
    restored = restore_runtime_state_checkpoint(
        json.loads(rows[-1]["runtime_state_checkpoint"]),
        expected_action_id="action-1",
        expected_suggestion_id=None,
        expected_user_id="user-1",
    )
    assert restored["history_by_scope"] == state["history_by_scope"]
    assert restored["llm_steps_taken"] == 1
    assert restored["tool_steps_taken"] == 0
    assert restored.get("next_action") == state.get("next_action")
    if has_calls:
        pending = restored["next_action"]
        assert pending is not None and pending.batch is not None
        assert [call.origin.call_id for call in pending.batch.calls] == [
            "call-0",
            "call-1",
        ]
    history = restored["history_by_scope"]["S"]
    assert [entry["step_type"] for entry in history] == [
        StepType.USER_REQUEST,
        *([StepType.ASSISTANT_MESSAGE] * message_count),
        StepType.LLM_OUTPUT,
    ]
    assert [entry["step_number"] for entry in history] == list(
        range(1, 3 + message_count)
    )
    formatted = ActionAgentFormatter().format_history(restored)
    if has_messages:
        assert formatted.index("確認します。") < formatted.index("関連資料も調べます。")
        assert formatted.count("phase: commentary") == 2


@pytest.mark.asyncio
async def test_recording_failure_leaves_history_and_database_unpublished(
    tmp_path: Path,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    state = _state()
    turn = _prepare(state, has_messages=True, has_calls=True)
    before_recording = copy.deepcopy(state)
    with _connect(db_path) as connection:
        connection.execute(
            f"""CREATE TRIGGER reject_commentary BEFORE INSERT ON agent_action_steps
            WHEN NEW.step_id = '{turn.messages[1].step_id}'
            BEGIN SELECT RAISE(ABORT, 'commentary commit failed'); END"""
        )
    with pytest.raises(LLMStepPersistenceError, match="commentary commit failed"):
        await _record(db_path, state, turn)
    assert state == before_recording
    with _connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_action_steps").fetchone()[0]
            == 1
        )


def test_turn_owner_comes_from_the_latest_user_in_the_inference_state() -> None:
    state = project_persisted_user_request_step(
        _state(),
        step_id="adopted-followup",
        step_number=2,
        local_step_number=2,
        short_step_id="S-2-USER",
        request_text="先に関連資料を調べて",
        occurred_at=TIMESTAMP,
        history_phase="executing",
    )
    turn = _prepare(state, has_messages=True, has_calls=True)
    assert turn.user_step_id == "adopted-followup"
    assert turn.messages[0].step_number == 3

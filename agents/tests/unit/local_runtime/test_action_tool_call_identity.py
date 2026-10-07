"""A tool step must keep the call identity the model issued.

The projection reconstructs one assistant turn from the tool rows that share an
``llm_step_id`` and pairs each result with its ``call_id``. Losing either -- at
the first write, when an approval pause settles into the same row, or across a
checkpoint -- leaves the turn unsendable as structured items.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import common
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.builders import (
    _build_tool_history_entry,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    build_finalized_tool_step,
    record_tool_step,
)
from pantaray_agents.local_runtime.agent_state.action_repository import (
    LocalActionRepository,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_agents.schema.agent.base import StepStatusType
from pantaray_llm.contracts.conversation import OpenAiProviderTurn

from .test_action_subagent_spawn import TIMESTAMP, _connect, _runtime
from .test_action_turn_recording import LLM_STEP_ID, _state

BUSY_TIMEOUT_MS = 1_000
_IDENTITY = "api_key:openai:gpt-6-luna:0123456789abcdef"


def _agent(db_path: Path) -> ActionAgent:
    return cast(
        ActionAgent,
        SimpleNamespace(
            repository=LocalActionRepository(
                db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
            )
        ),
    )


async def _record_call(
    agent: ActionAgent,
    state: ActionAgentState,
    *,
    index: int,
    call_id: str,
    status: str = "success",
) -> None:
    """Persist one call of a batch the way the act node does."""

    local_step_number = index + 2
    await record_tool_step(
        agent,
        state,
        step_id=f"tool-step-{index}",
        action_id="action-1",
        step_number=local_step_number,
        step_name="tool::read",
        tool_args={"tool_id": "read", "args": {"path": f"a{index}.txt"}},
        result=build_finalized_tool_step(
            status=status,
            output=FinalizedToolOutput(
                output={"status": "success"},
                storage_kind="inline_json",
                owner_kind="action_step",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
            ),
        ),
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        goal_handle="S",
        user_id="user-1",
        short_step_id=f"S-{local_step_number}-TOOL",
        local_step_number=local_step_number,
        origin=ActionToolCallOrigin(llm_step_id=LLM_STEP_ID, call_id=call_id),
    )


@pytest.mark.asyncio
async def test_a_parallel_batch_records_one_row_per_call_under_one_turn(
    tmp_path: Path,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    agent = _agent(db_path)
    state = _state()

    for index, call_id in enumerate(("call_a", "call_b", "call_c")):
        await _record_call(agent, state, index=index, call_id=call_id)

    with _connect(db_path) as connection:
        rows = connection.execute(
            """SELECT step_id, call_id, llm_step_id FROM agent_action_steps
            WHERE step_type = 'tool_execution' ORDER BY step_number"""
        ).fetchall()
    assert [row["call_id"] for row in rows] == ["call_a", "call_b", "call_c"]
    assert {row["llm_step_id"] for row in rows} == {LLM_STEP_ID}


@pytest.mark.asyncio
async def test_an_approval_pause_keeps_its_call_id_when_the_call_settles(
    tmp_path: Path,
) -> None:
    """The pause and the resumed result are one row, written twice."""

    db_path, _ = _runtime(tmp_path, seed_think=False)
    agent = _agent(db_path)
    state = _state()

    await _record_call(agent, state, index=0, call_id="call_a", status="processing")
    await _record_call(agent, state, index=0, call_id="call_a")

    with _connect(db_path) as connection:
        rows = connection.execute(
            """SELECT status, call_id, llm_step_id FROM agent_action_steps
            WHERE step_type = 'tool_execution'"""
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == StepStatusType.SUCCESS
    assert rows[0]["call_id"] == "call_a"
    assert rows[0]["llm_step_id"] == LLM_STEP_ID


@pytest.mark.asyncio
async def test_a_provider_turn_survives_the_store(tmp_path: Path) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    turn = OpenAiProviderTurn(
        provider="openai",
        items=[
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
            {"type": "function_call", "call_id": "call_a", "name": "read"},
        ],
    )

    saved = await repository.save_action_step(
        step_id="think-step",
        action_id="action-1",
        step_number=2,
        step_name="supervisor_think",
        step_type="llm_output",
        llm_response_text="accepted",
        status=StepStatusType.SUCCESS,
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        goal_handle="S",
        user_id="user-1",
        short_step_id="S-2-THINK",
        local_step_number=2,
        provider_turn=ActionProviderTurnRecord(
            turn=turn, identity=_IDENTITY, fingerprint="prefix"
        ),
    )
    assert saved.error is None
    # A turn recorded before its prefix was (migration 0122) is never handed
    # back: nothing says where it may go.
    with _connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, local_step_number,
                short_step_id, step_type, step_name, status, goal_handle,
                provider_turn, provider_turn_identity, created_at
            ) VALUES ('legacy-step', 'action-1', 'user-1', 1, 1, 'S-1-THINK',
                'llm_output', 'supervisor_think', 'success', 'S', ?, ?, ?)""",
            (turn.model_dump_json(), _IDENTITY, TIMESTAMP),
        )

    reloaded = await repository.get_action_provider_turns(
        user_id="user-1", action_id="action-1", identity=_IDENTITY
    )
    assert reloaded.error is None
    assert reloaded.data is not None
    # Byte identity, not just equality: a turn that comes back with its keys in
    # another order is another assistant item, and the whole prompt cache read
    # of the run depends on it being the same one.
    assert {
        step_id: (stored.turn.model_dump_json(), stored.fingerprint)
        for step_id, stored in reloaded.data.items()
    } == {"think-step": (turn.model_dump_json(), "prefix")}

    # The opaque state is readable only by the account that issued it, so
    # another connection is handed none of it.
    other = await repository.get_action_provider_turns(
        user_id="user-1", action_id="action-1", identity="api_key:openai:model:other"
    )
    assert other.error is None
    assert other.data == {}


def test_a_checkpoint_carries_the_identity_and_still_reads_one_without_it() -> None:
    state = _state()
    common.append_history_entry(
        state,
        scope_handle="S",
        entry=_build_tool_history_entry(
            step_id="tool-step-0",
            step_number=2,
            phase="executing",
            summary="資料を読む。",
            tool_id="read",
            started_at=TIMESTAMP,
            completed_at=TIMESTAMP,
            output={"status": "success"},
            short_step_id="S-2-TOOL",
            origin=ActionToolCallOrigin(llm_step_id=LLM_STEP_ID, call_id="call_a"),
        ),
    )
    checkpoint = build_runtime_state_checkpoint(state)

    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id=None,
        expected_user_id="user-1",
    )
    entry = restored["history_by_scope"]["S"][-1]
    assert entry["call_id"] == "call_a"
    assert entry["llm_step_id"] == LLM_STEP_ID

    # A checkpoint written before this node ran has no identity on its tool rows;
    # the version is unchanged, so resume must still accept it.
    legacy = json.loads(json.dumps(checkpoint))
    legacy_entry = cast(dict[str, object], legacy["history_by_scope"]["S"][-1])
    del legacy_entry["call_id"]
    del legacy_entry["llm_step_id"]
    legacy_restored = restore_runtime_state_checkpoint(
        legacy,
        expected_action_id="action-1",
        expected_suggestion_id=None,
        expected_user_id="user-1",
    )
    legacy_history = legacy_restored["history_by_scope"]["S"][-1]
    assert "call_id" not in legacy_history
    assert "llm_step_id" not in legacy_history

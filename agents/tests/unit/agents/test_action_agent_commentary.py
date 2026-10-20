from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.test_action_agent_tool_batch_execution import (
    SQL_CALL,
    SQL_TOOL,
    _act,
    _build_fixture,
    _think,
    _turn,
)
from tests.unit.agents.test_action_agent_tool_batch_resume import _restore

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.action_step_emission import (
    emit_visible_action_step,
)
from pantaray_agents.agents.action_agent.runtime.steps.llm import (
    LLMStepPersistenceError,
)
from pantaray_agents.agents.core.mixins.llm_tool_use_mixin import ActionTurnReply
from pantaray_agents.application.action.cancellation_service import (
    ActionCancellationService,
)
from pantaray_agents.local_runtime.action_conversation.history_queries import (
    ActionHistoryAssistantRow,
    read_timeline_history,
)
from pantaray_agents.local_runtime.action_conversation.sqlite_visibility import (
    register_action_tool_visibility_sqlite,
)
from pantaray_agents.local_runtime.agent_state.action_repository import (
    LocalActionRepository,
)
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_draft,
    serialize_memory_draft,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.runtime.action_subagent_queue import (
    enqueue_action_subagent_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_action_subagent_job_payload,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.action_conversation import ActionStepEventPayload
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary

MESSAGE = "原因は設定ファイルの読み込み順でした。依存する設定も確認します。"
PROCESS_ID = "message-process"


async def _fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="message-action",
        allowed_tool_ids=(SQL_TOOL,),
    )
    db_path = tmp_path / "runtime.db"
    agent.repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO processes(process_id,user_id,kind,status,action_id,
               started_at,updated_at,heartbeat_at,next_event_seq,current_job_id)
               VALUES (?,?,'action','running',?,?,?,?,1,'message-job')""",
            (
                PROCESS_ID,
                request.user_id,
                request.action_id,
                state["started_at"],
                state["started_at"],
                state["started_at"],
            ),
        )
        connection.execute(
            """INSERT INTO jobs(job_id,user_id,job_type,process_id,status,attempt,
               claimed_by,claimed_at,heartbeat_at,scheduled_at,started_at,logical_key)
               VALUES ('message-job',?,'execute_action',?,'running',1,'test-worker',?,?,?,?,?)""",
            (
                request.user_id,
                PROCESS_ID,
                *([state["started_at"]] * 4),
                request.action_id,
            ),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,
               local_step_number,short_step_id,step_type,step_name,status,goal_handle,
               user_request_text,accepted_sequence,adopted_process_id,created_at)
               VALUES (?,?,?,1,1,'S-1-USER','user_request','user_request',
               'success','S',?,1,?,?)""",
            (
                request.user_step_id,
                request.action_id,
                request.user_id,
                request.user_message.content,
                PROCESS_ID,
                state["started_at"],
            ),
        )
    agent._generate_llm_action_turn = AsyncMock(
        return_value=_commentary_turn(with_tool=True)
    )
    return agent, runtime, state, request, db_path


def _commentary_turn(*, with_tool: bool = False) -> ActionTurnReply:
    response = LlmActionTurnResponse(
        mode="action_turn",
        messages=[
            LlmCommentary(phase="commentary", source_message_id="msg-1", text=MESSAGE)
        ],
        calls=_turn(SQL_CALL).response.calls if with_tool else [],
    )
    return ActionTurnReply(response=response, provider_turn=None)


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft_position", "commentary"),
    [
        ("only", MESSAGE),
        ("later", "回答の下書きを用意しました。"),
        ("dropped", MESSAGE),
    ],
)
async def test_draft_turn_suppresses_commentary_and_submits_after_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    draft_position: str,
    commentary: str,
) -> None:
    agent, runtime, state, request, db_path = await _fixture(monkeypatch, tmp_path)
    turn = _turn(("draft_final_answer", {"answer": MESSAGE}))
    if draft_position == "later":
        turn = _turn(SQL_CALL, ("draft_final_answer", {"answer": MESSAGE}))
    retry_turns = []
    if draft_position == "dropped":
        retry_turns.append(turn)
        turn = _turn(SQL_CALL, SQL_CALL)
        turn.response.dropped_call_names = ["draft_final_answer"]
    turn.response.messages = [
        LlmCommentary(
            phase="commentary", source_message_id="draft-msg", text=commentary
        )
    ]
    agent._generate_llm_action_turn = AsyncMock(
        side_effect=[turn, *retry_turns, _turn(("submit_final_answer", {}))]
    )
    events: list[ActionStepEventPayload] = []

    async def capture(event: ActionStepEventPayload) -> None:
        events.append(event)

    async def emit(event):
        await emit_visible_action_step(
            capture, action_id=request.action_id, process_id=PROCESS_ID, event=event
        )

    runtime.emit_action_step = emit
    with TraceContextManager(
        user_id=request.user_id,
        action_id=request.action_id,
        local_job_id="message-job",
        extra={"process_id": PROCESS_ID},
    ):
        state = await _think(agent, runtime, state)
        stored = _rows(
            db_path,
            "SELECT llm_response_text, runtime_state_checkpoint FROM agent_action_steps WHERE step_type='llm_output'",
        )[0]
        # Keep the model response as diagnostic evidence, not a conversation message.
        assert (
            json.loads(stored["llm_response_text"])["messages"][0]["text"] == commentary
        )
        state = _restore(json.loads(stored["runtime_state_checkpoint"]), request)
        assert not any(
            entry["step_type"] == StepType.ASSISTANT_MESSAGE
            for entry in state["history_by_scope"]["S"]
        )
        state = await _act(agent, runtime, state)
        if draft_position == "dropped":
            assert not state.get("supervisor_pending_final_answer")
            assert "draft_final_answer" in json.dumps(state["history_by_scope"]["S"])
            state = await _think(agent, runtime, state)
            state = await _act(agent, runtime, state)
        assert state["supervisor_pending_final_answer"] == MESSAGE
        assert not state["final_output"]
        state = await _think(agent, runtime, state)
        state = await _act(agent, runtime, state)

    assert state["status"] == "success" and state["final_output"] == MESSAGE
    assert not any(event.step_kind == "assistant" for event in events)
    assert not _rows(
        db_path, "SELECT * FROM agent_action_steps WHERE step_type='assistant_message'"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        register_action_tool_visibility_sqlite(connection)
        page = read_timeline_history(
            connection, request.user_id, request.action_id, None, 1
        )
    assert not any(isinstance(row, ActionHistoryAssistantRow) for row in page.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_after_message", [False, True])
async def test_message_is_durable_assistant_and_batch_continues_after_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interrupt_after_message: bool,
) -> None:
    agent, runtime, state, request, db_path = await _fixture(monkeypatch, tmp_path)
    events: list[ActionStepEventPayload] = []

    async def capture(event: ActionStepEventPayload) -> None:
        events.append(event)
        if event.step_kind == "assistant":
            rows = _rows(
                db_path,
                "SELECT * FROM agent_action_steps WHERE step_type='assistant_message'",
            )
            assert len(rows) == 1
            assert rows[0]["llm_response_text"] == MESSAGE
            assert rows[0]["adopted_process_id"] == PROCESS_ID
            assert rows[0]["tool_args"] is None and rows[0]["tool_output"] is None
            assert rows[0]["short_step_id"].endswith("-ASSISTANT")
            assert "content" not in event.model_dump()
            if interrupt_after_message:
                raise asyncio.CancelledError

    async def emit(event):
        await emit_visible_action_step(
            capture, action_id=request.action_id, process_id=PROCESS_ID, event=event
        )

    runtime.emit_action_step = emit
    with TraceContextManager(extra={"process_id": PROCESS_ID}):
        if interrupt_after_message:
            with pytest.raises(asyncio.CancelledError):
                await _think(agent, runtime, state)
            checkpoint = _rows(
                db_path,
                "SELECT runtime_state_checkpoint FROM agent_action_steps WHERE step_type='llm_output'",
            )[0][0]
            state = _restore(json.loads(checkpoint), request)
        else:
            state = await _think(agent, runtime, state)
        assert state["tool_steps_taken"] == 0
        state = await _act(agent, runtime, state)

    assistant = [
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.ASSISTANT_MESSAGE
    ]
    assert len(assistant) == 1 and assistant[0]["assistant_message_text"] == MESSAGE
    assert assistant[0]["tool_id"] is None
    assert assistant[0]["assistant_phase"] == "commentary"
    assert state["llm_steps_taken"] == 1 and state["tool_steps_taken"] == 1
    assert state["phase"] == "executing" and not state["final_output"]
    assert len([event for event in events if event.step_kind == "assistant"]) == 1
    assert (
        len(
            _rows(
                db_path,
                "SELECT * FROM agent_action_steps WHERE step_type='assistant_message'",
            )
        )
        == 1
    )
    assert (
        len(
            _rows(
                db_path,
                "SELECT * FROM agent_action_steps WHERE step_name='tool::memory_sql' AND status='success'",
            )
        )
        == 1
    )
    assert (
        len(
            _rows(
                db_path,
                "SELECT i.* FROM tool_invocations i JOIN agent_action_steps s ON s.step_id=i.step_id WHERE s.step_type='assistant_message'",
            )
        )
        == 0
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        register_action_tool_visibility_sqlite(connection)
        page = read_timeline_history(
            connection, request.user_id, request.action_id, None, 1
        )
    messages = [row for row in page.rows if isinstance(row, ActionHistoryAssistantRow)]
    assert len(messages) == 1 and messages[0].content == MESSAGE


@pytest.mark.asyncio
async def test_failed_turn_save_rolls_back_messages_checkpoint_and_pending_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, runtime, state, _, db_path = await _fixture(monkeypatch, tmp_path)
    emitted = AsyncMock()
    runtime.emit_action_step = emitted
    with sqlite3.connect(db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_message BEFORE INSERT ON agent_action_steps
            WHEN NEW.step_type='assistant_message' BEGIN SELECT RAISE(ABORT,'message write failed'); END""")
    with pytest.raises(LLMStepPersistenceError, match="message write failed"):
        await _think(agent, runtime, state)
    emitted.assert_not_awaited()
    assert not _rows(
        db_path, "SELECT * FROM agent_action_steps WHERE step_type!='user_request'"
    )
    assert state["next_action"] is None
    assert state["llm_steps_taken"] == 0 and state["step"] == 2
    assert [entry["step_type"] for entry in state["history_by_scope"]["S"]] == [
        StepType.USER_REQUEST
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER fail_message")
    state = await _think(agent, runtime, state)
    state = await _act(agent, runtime, state)
    assert state["tool_steps_taken"] == 1
    assert (
        len(
            _rows(
                db_path,
                "SELECT * FROM agent_action_steps WHERE step_type='assistant_message'",
            )
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_before_commit", [False, True])
async def test_pending_response_is_not_published_and_stop_fences_its_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stop_before_commit: bool,
) -> None:
    agent, runtime, state, _, db_path = await _fixture(monkeypatch, tmp_path)
    runtime.services = runtime.services.model_copy(
        update={
            "cancellation": ActionCancellationService(
                replace(agent._cancellation_service._deps, repository=agent.repository)
            )
        }
    )
    state["cancel_check_max_consecutive_failures"] = 3
    state["cancel_check_failure_grace_seconds"] = 30
    emitted = AsyncMock()
    runtime.emit_action_step = emitted
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_response(**_kwargs):
        entered.set()
        await release.wait()
        return _commentary_turn(with_tool=True)

    agent._generate_llm_action_turn = AsyncMock(side_effect=held_response)
    if stop_before_commit:
        save = agent.repository.save_action_step

        async def stop_then_save(*args, **kwargs):
            with sqlite3.connect(db_path) as connection:
                connection.execute("UPDATE agent_actions SET status='canceled'")
            return await save(*args, **kwargs)

        monkeypatch.setattr(agent.repository, "save_action_step", stop_then_save)
    pending = asyncio.create_task(_think(agent, runtime, state))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        emitted.assert_not_awaited()
        assert not _rows(
            db_path, "SELECT * FROM agent_action_steps WHERE step_type!='user_request'"
        )
    finally:
        release.set()
    state = await asyncio.wait_for(pending, timeout=5)
    assert agent._generate_llm_action_turn.await_count == 1
    assert state["tool_steps_taken"] == 0
    if stop_before_commit:
        assert state["status"] == "canceled" and state["next_action"] is None
        emitted.assert_not_awaited()
        assert not _rows(
            db_path, "SELECT * FROM agent_action_steps WHERE step_type!='user_request'"
        )
    else:
        assert state["status"] == "processing" and state["next_action"] is not None
        assert emitted.await_count == 1


@pytest.mark.asyncio
async def test_invalid_call_discards_its_commentary_before_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, runtime, state, _, db_path = await _fixture(monkeypatch, tmp_path)
    invalid = _commentary_turn(with_tool=True)
    invalid.response.calls[0].name = "not_an_allowed_tool"
    agent._generate_llm_action_turn = AsyncMock(side_effect=[invalid, _turn(SQL_CALL)])
    runtime.emit_action_step = AsyncMock()
    state = await _think(agent, runtime, state)
    assert state["next_action"] is not None
    runtime.emit_action_step.assert_not_awaited()
    assert not _rows(
        db_path, "SELECT * FROM agent_action_steps WHERE step_type='assistant_message'"
    )


def _trace(request):
    return TraceContextManager(
        user_id=request.user_id,
        action_id=request.action_id,
        local_job_id="message-job",
        extra={"process_id": PROCESS_ID},
    )


@pytest.mark.asyncio
async def test_reply_without_calls_ends_the_turn_with_its_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, runtime, state, request, db_path = await _fixture(monkeypatch, tmp_path)
    agent._generate_llm_action_turn = AsyncMock(return_value=_commentary_turn())
    with _trace(request):
        result = await _think(agent, runtime, state)

    assert runtime.route_from_think(result) == "finalize"
    assert result["status"] == "success" and result["final_output"] == MESSAGE
    # The terminal publishes this draft and requires its body to be the answer.
    draft = deserialize_memory_draft(result["supervisor_memory_draft"])
    assert [document.content for document in draft.documents] == [MESSAGE]
    # The reply stays in the history the next follow-up turn is sent with.
    assert [
        entry["assistant_message_text"]
        for entry in result["history_by_scope"]["S"]
        if entry["step_type"] == StepType.ASSISTANT_MESSAGE
    ] == [MESSAGE]
    checkpoint = _rows(
        db_path,
        "SELECT runtime_state_checkpoint FROM agent_action_steps WHERE step_type='llm_output'",
    )[0][0]
    assert runtime.route_from_resume(_restore(json.loads(checkpoint), request)) == (
        "finalize"
    )


@pytest.mark.asyncio
async def test_reply_while_a_subagent_runs_continues_with_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, runtime, state, request, db_path = await _fixture(monkeypatch, tmp_path)
    payload = build_action_subagent_job_payload(
        {
            "job_id": "child-job",
            "process_id": "child-process",
            "user_id": request.user_id,
            "action_id": request.action_id,
            "parent_process_id": PROCESS_ID,
            "inference_profile_id": "action.subagent.luna",
            "action_context": "# Workspace Paths\nparent context",
            "task": "inspect the module",
            "context_refs": [],
            "resource_claim_ids": [],
        }
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with immediate_transaction(connection):
            enqueue_action_subagent_job_in_connection(
                connection=connection,
                payload=payload,
                scheduled_at=state["started_at"],
            )
    agent._generate_llm_action_turn = AsyncMock(
        side_effect=[_commentary_turn(), _turn(SQL_CALL)]
    )
    with _trace(request):
        state = await _think(agent, runtime, state)
        reply_think = state["history_by_scope"]["S"][-1]
        assert runtime.route_from_think(state) == "think"
        assert state["status"] == "processing" and not state["final_output"]
        assert "call wait_subagents" in reply_think["result_line"]
        state = await _think(agent, runtime, state)

    assert state["next_action"] is not None
    # The reply reserved its own History Ref, so the next THINK cannot reuse it.
    assert (
        state["history_by_scope"]["S"][-1]["short_step_id"]
        != reply_think["short_step_id"]
    )


@pytest.mark.asyncio
async def test_reply_the_draft_cannot_hold_continues_and_keeps_the_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, runtime, state, request, _ = await _fixture(monkeypatch, tmp_path)
    earlier = serialize_memory_draft(
        create_memory_draft(
            user_id=request.user_id,
            owner_node_id="memory-message-action",
            base_revision_id=None,
            documents=(MemoryDocument("body.md", "Earlier draft"),),
        )
    )
    state["supervisor_pending_final_answer"] = "Earlier draft"
    state["supervisor_memory_draft"] = earlier
    reply = _commentary_turn()
    reply.response.messages = [
        LlmCommentary(
            phase="commentary",
            source_message_id="msg-ref",
            text="See the note. [[ref:ref_1]]",
        )
    ]
    agent._generate_llm_action_turn = AsyncMock(return_value=reply)
    with _trace(request):
        state = await _think(agent, runtime, state)

    assert runtime.route_from_think(state) == "think"
    assert state["status"] == "processing" and not state["final_output"]
    assert "use link_memory" in state["history_by_scope"]["S"][-1]["result_line"]
    assert state["supervisor_pending_final_answer"] == "Earlier draft"
    assert state["supervisor_memory_draft"] == earlier

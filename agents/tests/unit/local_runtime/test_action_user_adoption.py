from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.application.action.new_user_turn import (
    reset_state_for_new_user_turn,
)
from pantaray_agents.local_runtime.runtime import action_user_adoption as adoption
from pantaray_agents.local_runtime.runtime.db_execution_context import (
    bind_local_runtime_db_execution_context,
)
from pantaray_agents.schema.agent.action import ActionAgentRequest
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.tasks.action_user_message import serialize_action_user_message
from pantaray_agents.utils.trace_context import TraceContextManager

from .local_action_repository_support import (
    ACTION_ID,
    BUSY_TIMEOUT_MS,
    SUGGESTION_ID,
    USER_ID,
    bootstrap_action_repository_db,
)

PROCESS_ID = "process-1"
SUCCESSOR_PROCESS_ID = "process-2"
JOB_ID = "job-1"
ROOT_STEP_ID = "root-user"


def _setup_pending_users(
    tmp_path: Path,
) -> tuple[Path, ActionAgentRequest, dict[str, Any]]:
    db_path = bootstrap_action_repository_db(tmp_path)
    root_message = ActionUserMessageInput(message_id="message-root", content="root")
    step_messages = (
        (ROOT_STEP_ID, root_message, 1, "2026-08-29T00:01:00Z", 1, 1, None),
        (
            "pending-2",
            ActionUserMessageInput(message_id="message-2", content="second"),
            3,
            "2026-08-29T00:03:00Z",
            None,
            None,
            PROCESS_ID,
        ),
        (
            "pending-1",
            ActionUserMessageInput(message_id="message-1", content="first"),
            2,
            "2026-08-29T00:02:00Z",
            None,
            None,
            PROCESS_ID,
        ),
    )
    payload = {
        "job_id": JOB_ID,
        "process_id": PROCESS_ID,
        "action_id": ACTION_ID,
        "user_id": USER_ID,
        "continuation_ref": {"kind": "user_step", "user_step_id": ROOT_STEP_ID},
    }
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE agent_actions SET status='processing' WHERE action_id=?",
            (ACTION_ID,),
        )
        connection.execute(
            """INSERT INTO processes(
                process_id,user_id,kind,status,action_id,suggestion_id,current_job_id,
                next_event_seq,started_at,updated_at,heartbeat_at
            ) VALUES (?,?,'action','running',?,?,?,1,
                      '2026-08-29T00:01:00Z','2026-08-29T00:01:00Z',
                      '2026-08-29T00:01:00Z')""",
            (PROCESS_ID, USER_ID, ACTION_ID, SUGGESTION_ID, JOB_ID),
        )
        connection.execute(
            """INSERT INTO jobs(
                job_id,user_id,job_type,process_id,status,logical_key,attempt,
                scheduled_at,started_at
            ) VALUES (?,?,'execute_action',?,'running',?,1,
                      '2026-08-29T00:01:00Z','2026-08-29T00:01:00Z')""",
            (JOB_ID, USER_ID, PROCESS_ID, ACTION_ID),
        )
        connection.execute(
            "INSERT INTO job_payloads(job_id,payload_json) VALUES (?,?)",
            (JOB_ID, json.dumps(payload)),
        )
        connection.executemany(
            """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,goal_handle,retry_count,prompt_tokens,
                completion_tokens,user_message_id,user_message_json,user_request_text,
                accepted_sequence,expected_process_id,adopted_process_id,
                started_at,completed_at,created_at
            ) VALUES (?,?,?,?,? ,?,'user_request','user_request','success','S',0,0,0,
                      ?,?,?,?,?,?,?,?,?)""",
            (
                (
                    step_id,
                    ACTION_ID,
                    USER_ID,
                    step_number,
                    local_step_number,
                    f"S-{local_step_number}-USER" if local_step_number else None,
                    message.message_id,
                    serialize_action_user_message(message),
                    message.content,
                    sequence,
                    expected_process_id,
                    PROCESS_ID if step_number else None,
                    created_at if step_number else None,
                    created_at if step_number else None,
                    created_at,
                )
                for (
                    step_id,
                    message,
                    sequence,
                    created_at,
                    step_number,
                    local_step_number,
                    expected_process_id,
                ) in step_messages
            ),
        )
    request = ActionAgentRequest(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        action_id=ACTION_ID,
        user_step_id=ROOT_STEP_ID,
        user_step_number=1,
        user_step_local_step_number=1,
        user_step_short_id="S-1-USER",
        user_step_created_at="2026-08-29T00:01:00Z",
        user_message=root_message,
    )
    state = create_initial_state(
        user_id=USER_ID,
        suggestion_id=SUGGESTION_ID,
        action_id=ACTION_ID,
        started_at="2026-08-29T00:01:00Z",
        max_steps=20,
        max_tool_steps=10,
        token_budget=None,
    )
    state["phase"] = "executing"
    projected = project_persisted_user_request_step(
        state,
        step_id=ROOT_STEP_ID,
        step_number=1,
        local_step_number=1,
        short_step_id="S-1-USER",
        request_text="root",
        occurred_at="2026-08-29T00:01:00Z",
        history_phase="executing",
    )
    return db_path, request, cast(dict[str, Any], projected)


@pytest.mark.asyncio
async def test_think_adopts_pending_users_in_acceptance_order_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, request, state = _setup_pending_users(tmp_path)
    observed: list[dict[str, Any]] = []
    transaction_steps: list[str] = []
    original_transaction = adoption.immediate_transaction

    async def capture_think(
        _runtime: ActionGraphRuntime, projected: dict[str, Any], _operation: object
    ) -> dict[str, Any]:
        observed.append(projected)
        return projected

    @contextmanager
    def observe_transaction(connection: sqlite3.Connection) -> Iterator[None]:
        with original_transaction(connection):
            transaction_steps.append("entered")
            yield

    def observe_clock() -> str:
        transaction_steps.append("clock")
        return "2026-08-29T00:04:00Z"

    monkeypatch.setattr(ActionGraphRuntime, "_run_token_sink_node", capture_think)
    monkeypatch.setattr(adoption, "immediate_transaction", observe_transaction)
    monkeypatch.setattr(adoption, "now_utc_iso", observe_clock)
    runtime = ActionGraphRuntime(
        agent=cast(Any, object()),
        request=request,
        state_config=cast(Any, {}),
        emit_action_step=cast(Any, None),
        emit_error=cast(Any, None),
        services=cast(
            Any,
            SimpleNamespace(
                cancellation=SimpleNamespace(
                    check_cancellation=AsyncMock(return_value=False)
                )
            ),
        ),
    )
    with (
        bind_local_runtime_db_execution_context(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
        ),
        TraceContextManager(
            action_id=ACTION_ID,
            user_id=USER_ID,
            local_job_id=JOB_ID,
            extra={"process_id": PROCESS_ID},
        ),
    ):
        result = await runtime.think(cast(Any, state))
        replay = await runtime.think(result)

    assert observed == [result, result]
    assert replay is result
    assert transaction_steps == ["entered", "clock", "entered", "clock"]
    assert [
        entry["user_request_text"] for entry in result["history_by_scope"]["S"]
    ] == [
        "root",
        "first",
        "second",
    ]
    assert state["step"] == 2
    assert result["step"] == 4
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT step_id,parent_step_id,step_number,local_step_number,
                      short_step_id,adopted_process_id,runtime_state_checkpoint
               FROM agent_action_steps ORDER BY step_number"""
        ).fetchall()
        events = connection.execute(
            """SELECT json_extract(payload,'$.data.step_id')
               FROM agent_process_events WHERE event_name='action_message_adopted'
               ORDER BY sequence"""
        ).fetchall()
    assert [row[:6] for row in rows] == [
        (ROOT_STEP_ID, None, 1, 1, "S-1-USER", PROCESS_ID),
        ("pending-1", ROOT_STEP_ID, 2, 2, "S-2-USER", PROCESS_ID),
        ("pending-2", "pending-1", 3, 3, "S-3-USER", PROCESS_ID),
    ]
    assert rows[1][6] is None
    assert json.loads(rows[2][6])["step"] == 4
    assert events == [("pending-1",), ("pending-2",)]


def test_in_connection_adoption_projects_pending_users_to_a_distinct_process(
    tmp_path: Path,
) -> None:
    db_path, _, state = _setup_pending_users(tmp_path)
    reset_state = reset_state_for_new_user_turn(
        cast(Any, state),
        started_at="2026-08-29T00:04:00Z",
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE processes SET status='completed', completed_at=updated_at "
            "WHERE process_id=?",
            (PROCESS_ID,),
        )
        connection.execute(
            """INSERT INTO processes(
                process_id,user_id,kind,status,action_id,suggestion_id,current_job_id,
                next_event_seq,started_at,updated_at,heartbeat_at
            ) VALUES (?,?,'action','running',?,?,NULL,1,
                      '2026-08-29T00:04:00Z','2026-08-29T00:04:00Z',
                      '2026-08-29T00:04:00Z')""",
            (SUCCESSOR_PROCESS_ID, USER_ID, ACTION_ID, SUGGESTION_ID),
        )
        # The previous run's checkpoint is superseded by the adopted turn's.
        connection.execute(
            "UPDATE agent_action_steps SET runtime_state_checkpoint='{}', "
            "runtime_state_checkpoint_version=1 WHERE step_id=?",
            (ROOT_STEP_ID,),
        )
        result = adoption.adopt_pending_action_user_steps_in_connection(
            connection=connection,
            user_id=USER_ID,
            action_id=ACTION_ID,
            expected_process_id=PROCESS_ID,
            adopted_process_id=SUCCESSOR_PROCESS_ID,
            state=reset_state,
            adopted_at="2026-08-29T00:04:00Z",
        )
        ownership = connection.execute(
            """SELECT expected_process_id,adopted_process_id,runtime_state_checkpoint
               FROM agent_action_steps WHERE step_number IN (2,3)
               ORDER BY step_number"""
        ).fetchall()
        checkpoint_steps = connection.execute(
            """SELECT step_id FROM agent_action_steps
               WHERE runtime_state_checkpoint IS NOT NULL"""
        ).fetchall()
        event_processes = connection.execute(
            """SELECT json_extract(payload,'$.data.process_id')
               FROM agent_process_events WHERE event_name='action_message_adopted'
               ORDER BY sequence"""
        ).fetchall()

    assert result["step"] == 4
    assert [entry["step_id"] for entry in result["history_by_scope"]["S"]] == [
        ROOT_STEP_ID,
        "pending-1",
        "pending-2",
    ]
    assert [tuple(row[:2]) for row in ownership] == [
        (PROCESS_ID, SUCCESSOR_PROCESS_ID),
        (PROCESS_ID, SUCCESSOR_PROCESS_ID),
    ]
    assert ownership[0][2] is None
    assert ownership[1][2] is not None
    assert [row[0] for row in checkpoint_steps] == ["pending-2"]
    assert [row[0] for row in event_processes] == [
        SUCCESSOR_PROCESS_ID,
        SUCCESSOR_PROCESS_ID,
    ]


@pytest.mark.asyncio
async def test_think_stops_before_adoption_when_action_is_canceled(
    tmp_path: Path,
) -> None:
    db_path, request, state = _setup_pending_users(tmp_path)
    check_cancellation = AsyncMock(return_value=True)
    runtime = ActionGraphRuntime(
        agent=cast(Any, object()),
        request=request,
        state_config=cast(Any, {}),
        emit_action_step=cast(Any, None),
        emit_error=cast(Any, None),
        services=cast(
            Any,
            SimpleNamespace(
                cancellation=SimpleNamespace(check_cancellation=check_cancellation)
            ),
        ),
    )

    result = await runtime.think(cast(Any, state))

    assert result is state
    check_cancellation.assert_awaited_once_with(state)
    with sqlite3.connect(db_path) as connection:
        pending = connection.execute(
            """SELECT COUNT(*) FROM agent_action_steps
               WHERE step_number IS NULL AND adopted_process_id IS NULL"""
        ).fetchone()[0]
    assert pending == 2


@pytest.mark.asyncio
async def test_think_does_not_adopt_when_parent_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, request, state = _setup_pending_users(tmp_path)
    state["llm_steps_taken"] = state["max_steps"]

    async def preserve_budget_error_path(
        _runtime: ActionGraphRuntime, projected: dict[str, Any], _operation: object
    ) -> dict[str, Any]:
        return projected

    monkeypatch.setattr(
        ActionGraphRuntime, "_run_token_sink_node", preserve_budget_error_path
    )
    runtime = ActionGraphRuntime(
        agent=cast(Any, object()),
        request=request,
        state_config=cast(Any, {}),
        emit_action_step=cast(Any, None),
        emit_error=cast(Any, None),
        services=cast(
            Any,
            SimpleNamespace(
                cancellation=SimpleNamespace(
                    check_cancellation=AsyncMock(return_value=False)
                )
            ),
        ),
    )
    with (
        bind_local_runtime_db_execution_context(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
        ),
        TraceContextManager(
            action_id=ACTION_ID,
            user_id=USER_ID,
            local_job_id=JOB_ID,
            extra={"process_id": PROCESS_ID},
        ),
    ):
        result = await runtime.think(cast(Any, state))

    assert result is state
    with sqlite3.connect(db_path) as connection:
        pending = connection.execute(
            """SELECT COUNT(*) FROM agent_action_steps
               WHERE step_number IS NULL AND adopted_process_id IS NULL"""
        ).fetchone()[0]
    assert pending == 2


def test_second_adoption_event_failure_rolls_back_the_whole_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, request, state = _setup_pending_users(tmp_path)
    original_append = adoption.append_action_invalidation_event
    calls = 0

    def fail_second_event(**kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("event append failed")
        return original_append(**cast(Any, kwargs))

    monkeypatch.setattr(adoption, "append_action_invalidation_event", fail_second_event)
    with pytest.raises(RuntimeError, match="event append failed"):
        adoption.adopt_pending_action_user_steps_at_parent_think(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            job_id=JOB_ID,
            process_id=PROCESS_ID,
            request=request,
            state=cast(Any, state),
        )
    with sqlite3.connect(db_path) as connection:
        pending = connection.execute(
            """SELECT COUNT(*) FROM agent_action_steps
               WHERE step_number IS NULL AND adopted_process_id IS NULL"""
        ).fetchone()[0]
        events = connection.execute(
            "SELECT COUNT(*) FROM agent_process_events"
        ).fetchone()[0]
    assert (pending, events) == (2, 0)

    monkeypatch.setattr(adoption, "append_action_invalidation_event", original_append)
    result = adoption.adopt_pending_action_user_steps_at_parent_think(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        job_id=JOB_ID,
        process_id=PROCESS_ID,
        request=request,
        state=cast(Any, state),
    )
    assert result["step"] == 4

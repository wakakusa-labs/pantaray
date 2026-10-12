from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.agent_state.action_repository import (
    LocalActionRepository,
)
from pantaray_agents.local_runtime.runtime.action_assistant_messages import (
    read_preceding_assistant_messages,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.action_assistant_message import (
    ActionAssistantMessageStep,
    ActionLlmTurnCommit,
)
from pantaray_agents.schema.agent.base import JSONValue, StepStatusType
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    RepositoryErrorKind,
    RepositoryResult,
)

from .action_seed import insert_agent_action
from .test_action_subagent_spawn import TIMESTAMP, _connect, _runtime


def _turn() -> ActionLlmTurnCommit:
    return ActionLlmTurnCommit(
        user_step_id="root-user-step",
        messages=tuple(
            ActionAssistantMessageStep(
                step_id=f"message-{index}",
                step_number=index + 1,
                local_step_number=index + 1,
                short_step_id=f"S-{index + 1}-ASSISTANT",
                content=text,
                created_at=TIMESTAMP,
            )
            for index, text in enumerate(
                ("確認します。", "関連資料も参照します。"), start=1
            )
        ),
    )


async def _save(
    repo: LocalActionRepository,
    turn: ActionLlmTurnCommit,
    *,
    checkpoint: dict[str, JSONValue] | None = None,
    response_text: str = "accepted response",
    think_step_number: int = 4,
) -> RepositoryResult[DBRow]:
    return await repo.save_action_step(
        step_id="think-1",
        user_id="user-1",
        action_id="action-1",
        step_number=think_step_number,
        local_step_number=4,
        short_step_id="S-4-THINK",
        step_type=StepType.LLM_OUTPUT,
        step_name="supervisor_think",
        status=StepStatusType.SUCCESS,
        goal_handle="S",
        parent_step_id="root-user-step",
        llm_response_text=response_text,
        runtime_state_checkpoint=(
            checkpoint if checkpoint is not None else {"step": 5, "next_action": None}
        ),
        runtime_state_checkpoint_version=4,
        started_at=TIMESTAMP,
        completed_at=TIMESTAMP,
        llm_turn=turn,
    )


@pytest.mark.asyncio
async def test_turn_commits_with_checkpoint_and_is_readable_once_after_reopen(
    tmp_path: Path,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    turn = _turn()

    first = await _save(repo, turn)
    assert first.error is None
    assert (await _save(repo, turn)).error is None

    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM agent_action_steps WHERE step_id <> 'root-user-step' "
            "ORDER BY step_number"
        ).fetchall()
        messages = read_preceding_assistant_messages(
            connection, user_id="user-1", action_id="action-1", before_step_number=5
        )
    assert [row["step_type"] for row in rows] == [
        "assistant_message",
        "assistant_message",
        "llm_output",
    ]
    assert [message.content for message in messages] == [
        message.content for message in turn.messages
    ]
    assert [message.assistant_phase for message in messages] == [
        "commentary",
        "commentary",
    ]
    assert all(row["parent_step_id"] == "think-1" for row in rows[:2])
    assert all(row["adopted_process_id"] == "parent-process" for row in rows[:2])
    assert all(row["runtime_state_checkpoint"] is None for row in rows[:2])
    assert json.loads(rows[2]["runtime_state_checkpoint"]) == {
        "step": 5,
        "next_action": None,
    }


@pytest.mark.asyncio
async def test_second_message_failure_rolls_back_output_and_all_messages(
    tmp_path: Path,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    with _connect(db_path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_second_message BEFORE INSERT ON agent_action_steps
            WHEN NEW.step_id = 'message-2'
            BEGIN SELECT RAISE(ABORT, 'message write failed'); END"""
        )
    result = await _save(repo, _turn())
    assert result.error == "message write failed"
    assert result.retryable is False
    with _connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_action_steps").fetchone()[0]
            == 1
        )
        connection.execute("DROP TRIGGER fail_second_message")
    assert (await _save(repo, _turn())).error is None


@pytest.mark.asyncio
async def test_turn_waits_for_a_concurrent_writer_instead_of_failing(
    tmp_path: Path,
) -> None:
    # The turn reads before it writes; a deferred transaction would fail at
    # once with "database is locked" while another connection holds the lock.
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=5000)
    lock_held = threading.Event()

    def hold_write_lock() -> None:
        with closing(sqlite3.connect(db_path, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE agent_actions SET updated_at = updated_at WHERE action_id = ?",
                ("action-1",),
            )
            lock_held.set()
            time.sleep(0.5)
            connection.execute("COMMIT")

    holder = threading.Thread(target=hold_write_lock)
    holder.start()
    try:
        assert lock_held.wait(timeout=5)
        result = await _save(repo, _turn())
    finally:
        holder.join()

    assert result.error is None
    assert result.data is not None and result.data["step_id"] == "think-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["message", "output", "checkpoint", "message_set"])
async def test_committed_response_identity_cannot_be_rewritten(
    tmp_path: Path,
    change: str,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    turn = _turn()
    assert (await _save(repo, turn)).error is None
    if change == "message":
        turn = replace(
            turn,
            messages=(
                turn.messages[0].model_copy(update={"content": "replacement"}),
                turn.messages[1],
            ),
        )
    elif change == "message_set":
        turn = replace(turn, messages=turn.messages[:1])
    result = await _save(
        repo,
        turn,
        response_text="changed output" if change == "output" else "accepted response",
        checkpoint={"step": 99} if change == "checkpoint" else None,
    )
    assert result.error_kind is RepositoryErrorKind.CONSTRAINT
    assert result.retryable is False
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT step_id, llm_response_text FROM agent_action_steps "
            "WHERE step_id <> 'root-user-step' ORDER BY step_number"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("message-1", "確認します。"),
        ("message-2", "関連資料も参照します。"),
        ("think-1", "accepted response"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [False, True])
@pytest.mark.parametrize(
    "fence", ["stop", "action_canceled", "process_ended", "new_user"]
)
async def test_response_after_stop_or_user_replacement_is_not_adopted(
    tmp_path: Path,
    fence: str,
    messages: bool,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    with _connect(db_path) as connection:
        if fence == "stop":
            connection.execute("UPDATE jobs SET cancel_requested_at = ?", (TIMESTAMP,))
        elif fence == "action_canceled":
            connection.execute("UPDATE agent_actions SET status = 'canceled'")
        elif fence == "process_ended":
            connection.execute("UPDATE processes SET status = 'completed'")
        else:
            connection.execute(
                """INSERT INTO agent_action_steps(
                step_id,action_id,user_id,step_number,local_step_number,short_step_id,
                step_type,step_name,status,goal_handle,user_request_text,
                accepted_sequence,adopted_process_id,created_at)
                VALUES ('new-user','action-1','user-1',2,2,'S-2-USER',
                'user_request','user_request','success','S','new request',2,
                'parent-process',?)""",
                (TIMESTAMP,),
            )
    turn = _turn()
    result = await _save(repo, turn if messages else replace(turn, messages=()))
    assert result.error == "Action turn USER owner is no longer active"
    assert result.retryable is False
    with _connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_action_steps WHERE step_type <> 'user_request'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_wrong_user_step_owner_does_not_publish_to_another_action(
    tmp_path: Path,
) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    insert_agent_action(
        db_path=db_path,
        user_id="other-user",
        action_id="other-action",
        suggestion_id="other-suggestion",
    )
    with _connect(db_path) as connection:
        connection.execute(
            """INSERT INTO processes(process_id,user_id,kind,status,action_id,
            started_at,updated_at,heartbeat_at,next_event_seq)
            VALUES ('other-process','other-user','action','running','other-action',
            ?,?,?,1)""",
            (TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """INSERT INTO agent_action_steps(step_id,action_id,user_id,step_number,
            local_step_number,short_step_id,step_type,step_name,status,goal_handle,
            user_request_text,accepted_sequence,adopted_process_id,created_at)
            VALUES ('other-user-step','other-action','other-user',1,1,'S-1-USER',
            'user_request','user_request','success','S','private request',1,
            'other-process',?)""",
            (TIMESTAMP,),
        )
    result = await _save(repo, replace(_turn(), user_step_id="other-user-step"))
    assert result.error_kind is RepositoryErrorKind.CONSTRAINT
    with _connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_action_steps").fetchone()[0]
            == 2
        )


@pytest.mark.asyncio
async def test_tool_only_turn_cannot_occupy_its_user_position(tmp_path: Path) -> None:
    db_path, _ = _runtime(tmp_path, seed_think=False)
    repo = LocalActionRepository(db_path=db_path, busy_timeout_ms=1000)
    result = await _save(repo, replace(_turn(), messages=()), think_step_number=1)
    assert result.error == "Action THINK must follow its adopted USER"
    with _connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM agent_action_steps").fetchone()[0]
            == 1
        )

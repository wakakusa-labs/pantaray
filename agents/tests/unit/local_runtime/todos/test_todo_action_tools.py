from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.helpers import (
    _resolve_act_tool_contract,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    todo as todo_runtime,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.todo import (
    run_todo_tool,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import TODO_TOOL_IDS, TOOL_REGISTRY
from pantaray_agents.local_runtime.todos import core, pickup
from pantaray_agents.local_runtime.todos.conversation import todo_message_id
from pantaray_agents.local_runtime.todos.model import (
    AiReport,
    Todo,
    TodoCheck,
    TodoDue,
)
from pantaray_agents.local_runtime.todos.store import (
    create_todo_item,
    read_todo,
    read_todo_for_action,
    update_todo,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import TraceContextManager

from .store_support import NOW, USER, execute, seed_action

WEEKLY = TodoDue(date="2026-11-08", repeat="weekly")


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from pantaray_agents.local_runtime.todos import store

    monkeypatch.setattr(store, "read_todo_clock", lambda: NOW)
    monkeypatch.setattr(pickup, "read_todo_clock", lambda: NOW)
    monkeypatch.setattr(todo_runtime, "read_todo_clock", lambda: NOW)


def _write(text: str, *, due: TodoDue | None = None) -> Todo:
    created = create_todo_item(
        user_id=USER,
        client_todo_id=text,
        project_id=None,
        text=text,
        week_of="2026-11-04",
        due=due,
        clock=NOW,
    )
    assert isinstance(created, Todo)
    return created


def _picked_action_id(db: Path, todo: Todo) -> str:
    pickup.pick_up_todos(user_id=USER)
    with closing(sqlite3.connect(db)) as connection:
        row = connection.execute(
            "SELECT action_id FROM agent_actions WHERE initial_user_message_id = ?",
            (todo_message_id(todo.todo_id),),
        ).fetchone()
    return str(row[0])


def _state(served_todo_id: str | None) -> ActionAgentState:
    return cast(
        ActionAgentState,
        {
            "user_id": USER,
            "action_id": "a",
            "phase": "executing",
            "context": {"served_todo_id": served_todo_id},
        },
    )


def _call(todo_id: str, tool_id: str, **args: JSONValue) -> JSONValue:
    with TraceContextManager(extra={"process_id": "run-1"}):
        result = run_todo_tool(
            step_id="step-1",
            tool_def=TOOL_REGISTRY[tool_id],
            args=args,
            state=_state(todo_id),
        )
    return result.output


def _stored(todo_id: str) -> Todo:
    entry = read_todo(user_id=USER, todo_id=todo_id)
    assert entry is not None
    return entry.todo


def test_an_action_finds_the_todo_it_was_started_or_accepted_for(db: Path) -> None:
    written = _write("Send the quote")
    picked_action = _picked_action_id(db, written)
    for todo_id, suggestion_id in (("todo-acc", "s-acc"), ("todo-rep", "s-rep")):
        execute(
            db,
            f"""INSERT INTO todo_items(todo_id, user_id, text, author, origin_kind,
              suggestion_id, week_start, created_at) VALUES ('{todo_id}', '{USER}',
              'Offer', 'ai', 'suggestion', '{suggestion_id}', '2026-11-02',
              '{NOW.now}')""",
        )
    seed_action(db, "accepted", message_id="m-1", suggestion_id="s-acc")
    seed_action(db, "replied", message_id="m-2", reply_to="s-rep")
    seed_action(db, "plain", message_id="m-3")

    assert [
        read_todo_for_action(user_id=USER, action_id=action_id)
        for action_id in (picked_action, "accepted", "replied", "plain", "missing")
    ] == [written.todo_id, "todo-acc", "todo-rep", None, None]


def test_only_a_todos_action_is_offered_the_todo_tools() -> None:
    _, plain_tools, _ = _resolve_act_tool_contract(_state(None))
    _, todo_tools, _ = _resolve_act_tool_contract(_state("todo-1"))

    assert not set(TODO_TOOL_IDS) & set(plain_tools)
    assert set(todo_tools) - set(plain_tools) == set(TODO_TOOL_IDS)


def test_done_checks_by_ai_and_adds_the_next_weekly_instance(db: Path) -> None:
    todo = _write("Water the plants", due=WEEKLY)

    assert _call(todo.todo_id, "update_todo", status="done", note="Watered.") == {
        "status": "checked"
    }

    assert _stored(todo.todo_id).check == TodoCheck(by="ai", at=NOW.now)
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT author, week_start, due_date, checked_at FROM todo_items "
            "WHERE previous_todo_id = ?",
            (todo.todo_id,),
        ).fetchall() == [("user", "2026-11-09", "2026-11-15", None)]


def test_a_report_carries_the_running_process_as_its_run(db: Path) -> None:
    todo = _write("Send the quote")

    output = _call(
        todo.todo_id, "update_todo", status="needs_user", note="Which\naddress?"
    )

    assert output == {"status": "reported"}
    assert _stored(todo.todo_id).ai_report == AiReport(
        kind="needs_user", note="Which address?", run_id="run-1", at=NOW.now
    )


def test_a_deletion_the_user_declined_cannot_be_requested_again(db: Path) -> None:
    todo = _write("Renew the domain")

    assert _call(todo.todo_id, "request_todo_deletion", reason="Renewed.") == {
        "status": "requested"
    }
    request = _stored(todo.todo_id).deletion_request
    assert request is not None and request.state == "pending"
    update_todo(
        user_id=USER,
        todo_id=todo.todo_id,
        step=lambda current: core.answer_deletion(
            current, request_id=request.request_id, answer="keep", now=NOW.now
        ),
    )

    assert _call(todo.todo_id, "request_todo_deletion", reason="Still renewed.") == {
        "status": "refused",
        "reason": "declined_before",
        "message": (
            "The user chose to keep this TODO, so its deletion cannot be "
            "requested again."
        ),
    }
    assert _stored(todo.todo_id).deletion is None


def test_a_purged_todo_is_reported_back_to_the_model(db: Path) -> None:
    assert _call("todo-gone", "update_todo", status="partial", note="Half.") == {
        "status": "refused",
        "reason": "not_found",
        "message": "This TODO no longer exists.",
    }


def test_a_progress_record_cites_a_tool_result_of_the_running_turn(db: Path) -> None:
    todo = _write("Close the Acme deal")
    seed_action(db, "a", message_id="m-1", run="running")
    execute(
        db,
        f"""INSERT INTO agent_action_steps(step_id, action_id, user_id, step_number,
          local_step_number, short_step_id, step_type, step_name, status,
          goal_handle, tool_output, created_at) VALUES ('step-2', 'a', '{USER}', 2,
          2, 'S-2-TOOL', 'tool_execution', 'tool::read', 'success', 'S', '{{}}',
          '{NOW.now}')""",
    )

    def record(*refs: str, step_id: str = "step-3") -> JSONValue:
        with TraceContextManager(extra={"process_id": "run-a"}):
            result = run_todo_tool(
                step_id=step_id,
                tool_def=TOOL_REGISTRY["record_progress"],
                args={
                    "quantity": 30_000_000,
                    "unit": "円",
                    "note": "Acme\nsigned",
                    "evidence_step_ids": list(refs),
                },
                state=_state(todo.todo_id),
            )
        return result.output

    assert record("S-1-THINK") == {
        "status": "refused",
        "reason": "evidence_not_in_run",
        "message": (
            "Each evidence id must be the History Ref of a successful tool call in "
            "this run. Nothing was recorded."
        ),
    }
    assert record("S-2-TOOL") == {"status": "recorded"}
    assert record("S-2-TOOL", step_id="step-4") == {
        "status": "already_recorded",
        "message": (
            "This figure is already recorded from the same tool results, so it was "
            "not added again."
        ),
    }
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT todo_id, quantity, note, author, evidence_action_id, "
            "evidence_step_ids FROM todo_progress_records"
        ).fetchall() == [
            (todo.todo_id, 30_000_000.0, "Acme signed", "ai", "a", '["step-2"]')
        ]

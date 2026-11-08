from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.agent_state import (
    suggestion_repository as suggestion_repository_module,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_TODO_PICKUP_REQUEST_TEXT,
)
from pantaray_agents.local_runtime.runtime.suggestion_acceptance import (
    SuggestionAcceptanceRejected,
    accept_suggestion,
)
from pantaray_agents.local_runtime.suggestion_state.repository import (
    LocalSuggestionStateRepository,
)
from pantaray_agents.local_runtime.todos import core, pickup
from pantaray_agents.local_runtime.todos.conversation import todo_message_id
from pantaray_agents.local_runtime.todos.display import (
    Partial,
    Working,
    display_state,
)
from pantaray_agents.local_runtime.todos.model import ActionConversation, Todo
from pantaray_agents.local_runtime.todos.reads import read_todo, read_todo_revision
from pantaray_agents.local_runtime.todos.writes import (
    create_todo_item,
    set_todo_check,
    suggestion_todo_id,
    update_todo,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
)
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentResponse,
    SuggestionInteractionContract,
    SuggestionTodo,
)

from .store_support import NOW, USER, T, execute


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pickup, "read_todo_clock", lambda: NOW)


def _write(
    text: str, *, week_of: str = "2026-11-04", project_id: str | None = "project-1"
) -> Todo:
    created = create_todo_item(
        user_id=USER,
        client_todo_id=text,
        project_id=project_id,
        text=text,
        week_of=week_of,
        due=None,
        clock=NOW,
    )
    assert isinstance(created, Todo)
    return created


def _insert_ai_todo(
    db: Path,
    todo_id: str,
    *,
    origin: str,
    report_kind: str | None = None,
) -> None:
    suggestion = f"'s-{todo_id}'" if origin == "suggestion" else "NULL"
    previous = "'todo-before'" if origin == "recurrence" else "NULL"
    due = "'2026-11-08', 'weekly'" if origin == "recurrence" else "NULL, NULL"
    report = (
        f"'{report_kind}', 'Draft is ready.', '{T}'"
        if report_kind
        else "NULL, NULL, NULL"
    )
    execute(
        db,
        f"""INSERT INTO todo_items(todo_id, user_id, project_id, text, author,
          origin_kind, suggestion_id, previous_todo_id, week_start, due_date,
          due_repeat, report_kind, report_note, report_at, created_at) VALUES
          ('{todo_id}', '{USER}', NULL, 'Renew the domain', 'ai', '{origin}',
           {suggestion}, {previous}, '2026-11-02', {due}, {report}, '{T}')""",
    )


def _actions(db: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """SELECT action.initial_user_message_id, action.initial_approval_mode,
                      step.step_name, step.user_message_json,
                      (SELECT llm_response_text FROM agent_action_steps
                       WHERE action_id = action.action_id
                         AND step_type = 'assistant_message') AS opening
               FROM agent_actions AS action JOIN agent_action_steps AS step
                 ON step.action_id = action.action_id
                AND step.user_message_id = action.initial_user_message_id
               ORDER BY action.created_at, action.action_id"""
        ).fetchall()


def test_a_written_todo_starts_one_action_with_its_text_and_project(
    db: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "sales"
    folder.mkdir()
    create_workspace_folder(
        db_path=db,
        busy_timeout_ms=1_000,
        user_id=USER,
        real_path=folder,
        display_name="sales",
        organization_ids=(),
        project_ids=("project-1",),
        now=T,
    )
    todo = _write("Send the quote")
    loose = _write("Call the bank", project_id=None)

    pickup.pick_up_todos(user_id=USER)

    rows = {row["initial_user_message_id"]: row for row in _actions(db)}
    assert set(rows) == {todo_message_id(todo.todo_id), todo_message_id(loose.todo_id)}
    row = rows[todo_message_id(todo.todo_id)]
    message = json.loads(row["user_message_json"])
    # The user wrote it, so it is their message and their approval setting.
    assert (row["step_name"], row["initial_approval_mode"], row["opening"]) == (
        "user_request",
        None,
        None,
    )
    assert message["content"] == "Send the quote"
    assert message["todo_handoff"] == {
        "todo_id": todo.todo_id,
        "project": {"display_name": "One", "paths": [str(folder.resolve())]},
    }
    loose_message = json.loads(
        rows[todo_message_id(loose.todo_id)]["user_message_json"]
    )
    assert loose_message["todo_handoff"] == {"todo_id": loose.todo_id, "project": None}


def test_an_ai_todo_continues_as_pantaray_and_asks_before_every_step(
    db: Path,
) -> None:
    _insert_ai_todo(db, "todo-recur", origin="recurrence")

    pickup.pick_up_todos(user_id=USER)

    [row] = _actions(db)
    message = json.loads(row["user_message_json"])
    assert (row["initial_user_message_id"], message["todo_handoff"]["todo_id"]) == (
        todo_message_id("todo-recur"),
        "todo-recur",
    )
    assert (
        row["step_name"],
        row["initial_approval_mode"],
        row["opening"],
        message["content"],
        message["todo_handoff"]["project"],
    ) == (
        "todo_pickup_request",
        "prompt_each_time",
        "Renew the domain",
        ACTION_TODO_PICKUP_REQUEST_TEXT,
        None,
    )


def test_only_open_todos_whose_week_began_are_picked(db: Path) -> None:
    _write("Next week", week_of="2026-11-10")
    checked = _write("Checked")
    set_todo_check(
        user_id=USER, todo_id=checked.todo_id, by="user", checked=True, clock=NOW
    )
    deleted = _write("Deleted")
    update_todo(
        user_id=USER,
        todo_id=deleted.todo_id,
        step=lambda todo: core.delete_by_user(todo, now=NOW.now),
    )
    carried = _write("From last week", week_of="2026-10-28")

    pickup.pick_up_todos(user_id=USER)

    assert [row["initial_user_message_id"] for row in _actions(db)] == [
        todo_message_id(carried.todo_id)
    ]


def test_passes_one_after_another_or_at_once_start_one_action_per_todo(
    db: Path,
) -> None:
    todo = _write("Send the quote")

    pickup.pick_up_todos(user_id=USER)
    pickup.pick_up_todos(user_id=USER)
    assert len(_actions(db)) == 1

    second = _write("Book the room")
    # Both passes read before either writes, then contend for the first write.
    with closing(sqlite3.connect(db, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        threads = [
            threading.Thread(target=pickup.pick_up_todos, kwargs={"user_id": USER})
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        blocker.execute("ROLLBACK")
        for thread in threads:
            thread.join()

    assert sorted(row["initial_user_message_id"] for row in _actions(db)) == sorted(
        todo_message_id(item.todo_id) for item in (todo, second)
    )


def test_a_failed_submit_skips_only_that_todo_and_is_retried_next_pass(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failing = _write("Private failing text")
    other = _write("Book the room")
    real_submit = pickup.submit_action_message

    def submit_failing_once(command):  # type: ignore[no-untyped-def]
        if command.message.message_id == todo_message_id(failing.todo_id):
            raise sqlite3.OperationalError("database is locked")
        return real_submit(command)

    monkeypatch.setattr(pickup, "submit_action_message", submit_failing_once)
    with caplog.at_level(logging.INFO, logger=pickup.__name__):
        pickup.pick_up_todos(user_id=USER)

    assert [row["initial_user_message_id"] for row in _actions(db)] == [
        todo_message_id(other.todo_id)
    ]
    assert f"todo_id={failing.todo_id} error=OperationalError" in caplog.text
    assert "Private failing text" not in caplog.text

    monkeypatch.setattr(pickup, "submit_action_message", real_submit)
    pickup.pick_up_todos(user_id=USER)
    assert len(_actions(db)) == 2


async def _save_suggestion(
    db: Path,
    suggestion_id: str,
    contract: SuggestionInteractionContract,
    todo: SuggestionTodo,
) -> LocalSuggestionRepository:
    """Save a shown Suggestion and its TODO as the Suggestion job does."""

    repository = LocalSuggestionRepository(
        db_path=str(db), busy_timeout_ms=1_000, activity_repository=object()
    )
    await repository.create_processing_suggestion_row(
        user_id=USER, suggestion_id=suggestion_id, created_at=T
    )
    saved = await repository.save_suggestion(
        SuggestionAgentResponse(
            suggestion_id=suggestion_id,
            user_id=USER,
            created_at=T,
            answer=f"Message of {suggestion_id}",
            suggestion_summary="summary",
            status=StatusType.SUCCESS,
            has_suggestion=True,
            interaction_contract=contract,
            todo=todo,
        ),
        prompt_name="suggestion",
        prompt_version="1",
    )
    assert saved.error is None
    return repository


def _pick_up_twice_at_once(db: Path) -> None:
    # Both passes read before either writes, then contend for the first write.
    with closing(sqlite3.connect(db, isolation_level=None)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        threads = [
            threading.Thread(target=pickup.pick_up_todos, kwargs={"user_id": USER})
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        blocker.execute("ROLLBACK")
        for thread in threads:
            thread.join()


@pytest.mark.asyncio
async def test_a_suggestion_left_to_continue_opens_one_action_with_its_message(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suggestion_repository_module, "read_todo_clock", lambda: NOW)
    line = "Send the reply"
    await _save_suggestion(
        db,
        "s-go",
        "action_offer",
        SuggestionTodo(text=line, outcome="continue", note="Not sent."),
    )
    await _save_suggestion(
        db,
        "s-part",
        "action_offer",
        SuggestionTodo(text=line, outcome="partial", note="Drafted."),
    )
    await _save_suggestion(
        db,
        "s-done",
        "action_offer",
        SuggestionTodo(text=line, outcome="done", note=None),
    )
    await _save_suggestion(
        db,
        "s-notice",
        "message_only",
        SuggestionTodo(text=line, outcome="done", note=None),
    )

    revision = read_todo_revision(user_id=USER)
    pickup.pick_up_todos(user_id=USER)
    # The open windows read the TODO list again, and the Suggestion with it.
    assert read_todo_revision(user_id=USER) == revision + 1
    pickup.pick_up_todos(user_id=USER)
    _pick_up_twice_at_once(db)
    assert read_todo_revision(user_id=USER) == revision + 1

    todo_id = suggestion_todo_id("s-go")
    [row] = _actions(db)
    message = json.loads(row["user_message_json"])
    assert (
        row["initial_user_message_id"],
        row["step_name"],
        row["initial_approval_mode"],
        row["opening"],
        message["content"],
        message["todo_handoff"],
    ) == (
        todo_message_id(todo_id),
        "todo_pickup_request",
        "prompt_each_time",
        "Message of s-go",
        ACTION_TODO_PICKUP_REQUEST_TEXT,
        {"todo_id": todo_id, "project": None},
    )
    with closing(sqlite3.connect(db)) as connection:
        linked = connection.execute(
            "SELECT action.suggestion_id, opening.source_suggestion_id "
            "FROM agent_action_steps AS opening JOIN agent_actions AS action "
            "ON action.action_id = opening.action_id "
            "WHERE opening.step_type = 'assistant_message'"
        ).fetchall()
        reactions = connection.execute(
            "SELECT user_reaction, accepted_at FROM agent_suggestions"
        ).fetchall()
    # Linked as a reply is, so nothing records an approval the user never gave.
    assert linked == [(None, "s-go")]
    assert set(reactions) == {(None, None)}
    entry = read_todo(user_id=USER, todo_id=todo_id)
    assert entry is not None
    assert isinstance(entry.conversation, ActionConversation)
    assert display_state(entry.todo, entry.conversation, NOW.current_week_start) == (
        Working(note=None)
    )
    # What a window showing the Suggestion reads, as after a reply: its Action,
    # and no reaction.
    state = await LocalSuggestionStateRepository(
        db_path=db, busy_timeout_ms=1_000
    ).get_suggestion_state(user_id=USER, suggestion_id="s-go")
    assert state.data is not None
    assert (state.data["action_id"], state.data["user_reaction"]) == (
        entry.conversation.action_id,
        None,
    )


@pytest.mark.asyncio
async def test_a_suggestion_pantaray_picked_up_cannot_be_approved_into_a_second_action(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suggestion_repository_module, "read_todo_clock", lambda: NOW)
    await _save_suggestion(
        db,
        "s-go",
        "action_offer",
        SuggestionTodo(text="Send the reply", outcome="continue", note="Not sent."),
    )
    pickup.pick_up_todos(user_id=USER)

    accepted = await accept_suggestion(
        user_id=USER,
        suggestion_id="s-go",
        command_id="11111111-1111-4111-8111-111111111111",
        approval_mode="prompt_each_time",
        language="ja",
        supplement=None,
        supplement_project_refs=(),
        supplement_work_refs=(),
        images=(),
        files=(),
    )

    assert accepted == SuggestionAcceptanceRejected("creation_conflict")
    assert len(_actions(db)) == 1


async def _dismiss(repository: LocalSuggestionRepository, suggestion_id: str):  # type: ignore[no-untyped-def]
    return await repository.mark_user_reaction(
        user_id=USER,
        suggestion_id=suggestion_id,
        user_reaction="dismiss",
        action_status="idle",
        rejected_at=NOW.now,
    )


@pytest.mark.asyncio
async def test_a_dismissed_suggestion_is_not_picked_up_and_stays_where_it_stopped(
    db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(suggestion_repository_module, "read_todo_clock", lambda: NOW)
    repository = await _save_suggestion(
        db,
        "s-go",
        "action_offer",
        SuggestionTodo(text="Send the reply", outcome="continue", note="Not sent."),
    )
    dismissed = await _dismiss(repository, "s-go")
    assert dismissed.error is None

    with caplog.at_level(logging.INFO, logger=pickup.__name__):
        pickup.pick_up_todos(user_id=USER)

    assert _actions(db) == []
    # Not tried and failed on every pass: it is not a candidate.
    assert "TODO pickup" not in caplog.text
    entry = read_todo(user_id=USER, todo_id=suggestion_todo_id("s-go"))
    assert entry is not None
    assert display_state(entry.todo, entry.conversation, NOW.current_week_start) == (
        Partial(note="Not sent.")
    )


@pytest.mark.asyncio
async def test_a_suggestion_pantaray_picked_up_cannot_be_dismissed(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suggestion_repository_module, "read_todo_clock", lambda: NOW)
    repository = await _save_suggestion(
        db,
        "s-go",
        "action_offer",
        SuggestionTodo(text="Send the reply", outcome="continue", note="Not sent."),
    )
    pickup.pick_up_todos(user_id=USER)

    dismissed = await _dismiss(repository, "s-go")

    assert (
        dismissed.error
        == suggestion_repository_module.SUGGESTION_HAS_CONVERSATION_ERROR
    )
    with closing(sqlite3.connect(db)) as connection:
        reaction = connection.execute(
            "SELECT user_reaction, rejected_at FROM agent_suggestions"
        ).fetchone()
        status = connection.execute("SELECT status FROM agent_actions").fetchone()
    assert (reaction, status) == ((None, None), ("queued",))

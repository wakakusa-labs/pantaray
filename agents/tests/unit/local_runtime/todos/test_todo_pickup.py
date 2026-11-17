from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    delete_history_item,
)
from pantaray_agents.local_runtime.agent_state import LocalSuggestionRepository
from pantaray_agents.local_runtime.agent_state import (
    suggestion_repository as suggestion_repository_module,
)
from pantaray_agents.local_runtime.runtime import action_messages
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ACTION_TODO_PICKUP_REQUEST_TEXT,
)
from pantaray_agents.local_runtime.runtime.suggestion_acceptance import (
    SuggestionAcceptanceResult,
    SuggestionAccepted,
    accept_suggestion,
)
from pantaray_agents.local_runtime.todos import core, pickup
from pantaray_agents.local_runtime.todos.conversation import todo_message_id
from pantaray_agents.local_runtime.todos.db import count_todo_change
from pantaray_agents.local_runtime.todos.display import (
    Partial,
    Waiting,
    display_state,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    Todo,
    WrittenOrigin,
)
from pantaray_agents.local_runtime.todos.reads import read_todo, read_todo_revision
from pantaray_agents.local_runtime.todos.writes import (
    create_todo_item,
    set_todo_check,
    suggestion_todo_id,
    update_todo,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
)
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.agent.suggestion import (
    SuggestionAgentResponse,
    SuggestionInteractionContract,
    SuggestionTargetContext,
    SuggestionTodo,
)

from .store_support import (
    IMAGE,
    NOW,
    USER,
    T,
    execute,
    stage_file,
    staged_path,
)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pickup, "read_todo_clock", lambda: NOW)


def _write(
    text: str,
    *,
    starts_on: str | None = "2026-11-04",
    project_id: str | None = "project-1",
) -> Todo:
    created = create_todo_item(
        user_id=USER,
        client_todo_id=text,
        project_id=project_id,
        organization_id=None,
        text=text,
        starts_on=starts_on,
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
          origin_kind, suggestion_id, previous_todo_id, starts_on, due_date,
          due_repeat, report_kind, report_note, report_at, created_at) VALUES
          ('{todo_id}', '{USER}', NULL, 'Renew the domain', 'ai', '{origin}',
           {suggestion}, {previous}, '2026-11-02', {due}, {report}, '{T}')""",
    )


def _actions(db: Path) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """SELECT action.initial_user_message_id,
                      step.step_name, step.user_message_json, step.user_request_text,
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
    # The user wrote it, so it is their message.
    assert (row["step_name"], row["opening"]) == ("user_request", None)
    assert message["content"] == "Send the quote"
    assert message["todo_handoff"] == {
        "todo_id": todo.todo_id,
        "project": {"display_name": "One", "paths": [str(folder.resolve())]},
    }
    loose_message = json.loads(
        rows[todo_message_id(loose.todo_id)]["user_message_json"]
    )
    assert loose_message["todo_handoff"] == {
        "todo_id": loose.todo_id,
        "project": None,
    }


def test_a_written_todo_hands_its_attachments_to_the_actions_first_message(
    db: Path, tmp_path: Path
) -> None:
    artifacts = tmp_path / "artifacts"
    file = stage_file(artifacts)
    created = create_todo_item(
        user_id=USER,
        client_todo_id="attached",
        project_id=None,
        organization_id=None,
        text="Summarize the notes",
        starts_on="2026-11-04",
        due=None,
        clock=NOW,
        files=(file,),
        images=(IMAGE,),
    )
    assert isinstance(created, Todo)

    pickup.pick_up_todos(user_id=USER)

    [row] = _actions(db)
    message = json.loads(row["user_message_json"])
    assert message["files"] == [file.model_dump()]
    assert message["images"] == [IMAGE.model_dump()]
    with closing(sqlite3.connect(db)) as connection:
        [(action_id,)] = connection.execute("SELECT action_id FROM agent_actions")
    workspace = resolve_action_storage_paths(
        db_path=db, user_id=USER, action_id=action_id
    ).workspace
    assert (workspace / file.workspace_path).read_bytes() == b"Meeting notes"
    assert not staged_path(artifacts, file).exists()
    # The Action owns them now: the TODO no longer holds them.
    entry = read_todo(user_id=USER, todo_id=created.todo_id)
    assert entry is not None and entry.todo.origin == WrittenOrigin()

    # Deleted with its conversation, then restored, the TODO starts again with
    # its text alone instead of failing on files that were already taken.
    update_todo(
        user_id=USER,
        todo_id=created.todo_id,
        step=lambda todo: core.delete_by_user(todo, now=NOW.now),
    )
    execute(
        db,
        "UPDATE agent_actions SET status = 'success'; "
        "UPDATE processes SET status = 'completed'; "
        "UPDATE jobs SET status = 'completed';",
    )
    delete_history_item(
        db_path=db,
        busy_timeout_ms=1_000,
        artifact_root=artifacts,
        user_id=USER,
        kind="conversation",
        item_id=action_id,
    )
    update_todo(
        user_id=USER,
        todo_id=created.todo_id,
        step=lambda todo: core.restore(todo, now=NOW.now),
    )

    pickup.pick_up_todos(user_id=USER)

    [restarted] = _actions(db)
    message = json.loads(restarted["user_message_json"])
    assert (message["content"], message["files"], message["images"]) == (
        "Summarize the notes",
        [],
        [],
    )


def test_an_ai_todo_continues_as_pantaray(db: Path) -> None:
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
        row["opening"],
        message["content"],
        message["todo_handoff"]["project"],
    ) == (
        "todo_pickup_request",
        "Renew the domain",
        ACTION_TODO_PICKUP_REQUEST_TEXT,
        None,
    )


def test_only_open_todos_whose_start_date_came_are_picked(db: Path) -> None:
    # Tomorrow, though in the week that holds today.
    _write("Tomorrow", starts_on="2026-11-05")
    _write("No start yet", starts_on=None)
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
    carried = _write("From last week", starts_on="2026-10-28")
    today = _write("Today")

    pickup.pick_up_todos(user_id=USER)

    assert {row["initial_user_message_id"] for row in _actions(db)} == {
        todo_message_id(carried.todo_id),
        todo_message_id(today.todo_id),
    }


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
            # What the shown message leaves out, as only the summary says it.
            suggestion_summary="Draft the reply only; do not send it.",
            target_context=SuggestionTargetContext(
                organization_name="Wakakusa", project_name="Pantaray"
            ),
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


@pytest.mark.asyncio
async def test_a_suggestion_left_to_continue_starts_only_when_the_user_approves_it(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(suggestion_repository_module, "read_todo_clock", lambda: NOW)
    await _save_suggestion(
        db,
        "s-go",
        "action_offer",
        SuggestionTodo(text="Send the reply", outcome="continue", note="Not sent."),
    )
    todo_id = suggestion_todo_id("s-go")
    revision = read_todo_revision(user_id=USER)

    pickup.pick_up_todos(user_id=USER)

    assert _actions(db) == []
    assert read_todo_revision(user_id=USER) == revision
    waiting = read_todo(user_id=USER, todo_id=todo_id)
    assert waiting is not None
    # Not "about to start": only the user's answer starts it.
    assert display_state(waiting.todo, waiting.conversation, NOW.today) == (
        Waiting(reason="approval", note="Not sent.")
    )

    accepted = await _approve("s-go")

    assert isinstance(accepted, SuggestionAccepted)
    entry = read_todo(user_id=USER, todo_id=todo_id)
    assert entry is not None
    assert isinstance(entry.conversation, ActionConversation)
    assert entry.conversation.action_id == accepted.action.action_id
    pickup.pick_up_todos(user_id=USER)
    assert len(_actions(db)) == 1


async def _approve(suggestion_id: str) -> SuggestionAcceptanceResult:
    return await accept_suggestion(
        user_id=USER,
        suggestion_id=suggestion_id,
        command_id="11111111-1111-4111-8111-111111111111",
        language="ja",
        supplement=None,
        supplement_project_refs=(),
        supplement_work_refs=(),
        images=(),
        files=(),
    )


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
    assert display_state(entry.todo, entry.conversation, NOW.today) == (
        Partial(note="Not sent.")
    )


def test_a_failed_change_count_leaves_no_action_and_the_next_pass_recovers(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_ai_todo(db, "todo-recur", origin="recurrence")
    revision = read_todo_revision(user_id=USER)

    def failing_count(connection, *, user_id):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(action_messages, "count_todo_change", failing_count)
    pickup.pick_up_todos(user_id=USER)

    # Counted with the Action or not at all: nothing was started, nothing announced.
    assert _actions(db) == []
    assert read_todo_revision(user_id=USER) == revision

    monkeypatch.setattr(action_messages, "count_todo_change", count_todo_change)
    pickup.pick_up_todos(user_id=USER)

    assert len(_actions(db)) == 1
    assert read_todo_revision(user_id=USER) == revision + 1

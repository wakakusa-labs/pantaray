from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.todos.conversation import (
    resolve_todo_conversations,
    todo_message_id,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    ActionRun,
    ActiveRun,
    ApprovalPendingRun,
    EndedRun,
    NoConversation,
    RecurrenceOrigin,
    SuggestionConversation,
    SuggestionOrigin,
    Todo,
    TodoConversation,
)

from .store_support import USER, seed_action, seed_suggestion
from .support import make_todo


def _resolve(db: Path, *todos: Todo) -> list[TodoConversation]:
    with closing(sqlite3.connect(db)) as connection:
        found = resolve_todo_conversations(connection, user_id=USER, todos=todos)
    return [found[todo.todo_id] for todo in todos]


@pytest.mark.parametrize(
    ("status", "run", "expected"),
    [
        ("success", "completed", EndedRun("run-a", "success", "Sent the quote.")),
        ("error", "failed", EndedRun("run-a", "error", None)),
        ("processing", "running", ActiveRun("run-a")),
        ("processing", "paused", ApprovalPendingRun("run-a")),
    ],
)
def test_a_written_todo_finds_the_action_opened_with_its_message_id(
    db: Path, status: str, run: str, expected: ActionRun
) -> None:
    written = make_todo(todo_id="todo-w")
    recurrence = make_todo(
        todo_id="todo-r", origin=RecurrenceOrigin(previous_todo_id="todo-w")
    )
    final_output = "" if status == "error" else "**Sent** the quote.\nDetails."
    seed_action(
        db,
        "a",
        message_id=todo_message_id("todo-w"),
        status=status,
        run=run,
        final_output=final_output,
    )

    assert _resolve(db, written, recurrence) == [
        ActionConversation(action_id="a", latest_run=expected),
        NoConversation(),
    ]


def test_a_suggestion_todo_is_the_suggestions_conversation_until_an_action_takes_it(
    db: Path,
) -> None:
    for suggestion_id in ("s-alone", "s-accepted", "s-replied", "s-picked"):
        seed_suggestion(db, suggestion_id)
    seed_action(db, "accepted", message_id="m-1", suggestion_id="s-accepted")
    seed_action(db, "replied", message_id="m-2", reply_to="s-replied")
    seed_action(db, "picked", message_id=todo_message_id("todo-picked"))
    todos = [
        make_todo(todo_id=f"todo-{name}", origin=SuggestionOrigin(f"s-{name}"))
        for name in ("alone", "accepted", "replied", "picked", "gone")
    ]

    assert [
        conversation.action_id
        if isinstance(conversation, ActionConversation)
        else conversation
        for conversation in _resolve(db, *todos)
    ] == [
        SuggestionConversation(suggestion_id="s-alone", answered=False),
        "accepted",
        "replied",
        "picked",
        # The Suggestion was deleted from history: nothing is left to open.
        NoConversation(),
    ]

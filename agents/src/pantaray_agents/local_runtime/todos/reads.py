"""Read stored TODOs, each list with the conversations that go with its TODOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.conversation import (
    resolve_todo_conversations,
)
from pantaray_agents.local_runtime.todos.db import (
    TODO_SELECT,
    TodoProjectFilter,
    TodoScope,
    read_todo_row,
    todo_db_connection,
    todo_from_row,
    todo_project_sql,
    todo_scope_sql,
)
from pantaray_agents.local_runtime.todos.model import (
    LocalDate,
    Todo,
    TodoConversation,
    TodoId,
)

type TodoListSort = Literal["added", "due"]

# Open TODOs come first, in the order asked for; checked ones follow, the last
# checked first. A NULL `checked_at` ties every open TODO on the second key.
_LIST_ORDER: Final[dict[TodoListSort, str]] = {
    "added": "",
    "due": "todo.due_date IS NULL, todo.due_date, ",
}


@dataclass(frozen=True, slots=True)
class TodoEntry:
    todo: Todo
    conversation: TodoConversation


@dataclass(frozen=True, slots=True)
class TodoList:
    today: LocalDate
    items: tuple[TodoEntry, ...]


def read_todo(*, user_id: str, todo_id: TodoId) -> TodoEntry | None:
    with todo_db_connection() as connection:
        todo = read_todo_row(connection, user_id=user_id, todo_id=todo_id)
        if todo is None:
            return None
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=(todo,)
        )
    return TodoEntry(todo=todo, conversation=conversations[todo.todo_id])


def read_todo_list(
    *,
    user_id: str,
    scope: TodoScope,
    project: TodoProjectFilter,
    sort: TodoListSort,
    clock: TodoClock,
) -> TodoList:
    """Every TODO not deleted; `sort` orders the open ones, by date added or
    by due date with no due date last."""

    scope_sql, scope_params = todo_scope_sql(scope)
    project_sql, project_params = todo_project_sql(project)
    # Design limit: every checked TODO is read too; page the list when a
    # project holds more than 1,000 TODOs.
    sql = (
        f"SELECT {TODO_SELECT} FROM todo_items AS todo "
        "WHERE todo.user_id = :user_id AND todo.deleted_at IS NULL "
        f"AND {scope_sql} AND {project_sql} "
        "ORDER BY todo.checked_at IS NOT NULL, todo.checked_at DESC, "
        f"{_LIST_ORDER[sort]}todo.created_at, todo.todo_id"
    )
    params = {"user_id": user_id, **scope_params, **project_params}
    with todo_db_connection() as connection:
        todos = [todo_from_row(row) for row in connection.execute(sql, params)]
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=todos
        )
    return TodoList(
        today=clock.today,
        items=tuple(
            TodoEntry(todo=todo, conversation=conversations[todo.todo_id])
            for todo in todos
        ),
    )


def read_deleted_todos(
    *, user_id: str, scope: TodoScope, clock: TodoClock
) -> tuple[Todo, ...]:
    """Deleted TODOs that can still be restored, most recently deleted first."""

    scope_sql, scope_params = todo_scope_sql(scope)
    with todo_db_connection() as connection:
        rows = connection.execute(
            f"SELECT {TODO_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = :user_id AND todo.deleted_at IS NOT NULL "
            f"AND {scope_sql} ORDER BY todo.deleted_at DESC, todo.todo_id",
            {"user_id": user_id, **scope_params},
        ).fetchall()
    return tuple(
        todo
        for todo in map(todo_from_row, rows)
        if not core.purge_due(todo, now=clock.now)
    )


def read_purge_due_todos(*, user_id: str, now: str) -> tuple[TodoEntry, ...]:
    """Deleted TODOs past retention, with the conversations that go with them."""

    with todo_db_connection() as connection:
        rows = connection.execute(
            f"SELECT {TODO_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.deleted_at IS NOT NULL",
            (user_id,),
        ).fetchall()
        due = [
            todo for todo in map(todo_from_row, rows) if core.purge_due(todo, now=now)
        ]
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=due
        )
    return tuple(
        TodoEntry(todo=todo, conversation=conversations[todo.todo_id]) for todo in due
    )


def read_pickup_candidates(*, user_id: str, today: LocalDate) -> tuple[TodoEntry, ...]:
    """Open TODOs whose start date has come, with their conversations.

    A superset of what the pickup starts: `pickup_target` decides.
    """

    # Design limit: every open TODO is read on each pass, those whose Action
    # already runs too, and the scan walks the user's TODOs started by today;
    # add a partial index on open TODOs when a pass exceeds 50 ms.
    with todo_db_connection() as connection:
        rows = connection.execute(
            f"SELECT {TODO_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.starts_on <= ? "
            "AND todo.checked_at IS NULL AND todo.deleted_at IS NULL "
            "ORDER BY todo.created_at, todo.todo_id",
            (user_id, today),
        ).fetchall()
        todos = [todo_from_row(row) for row in rows]
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=todos
        )
    return tuple(
        TodoEntry(todo=todo, conversation=conversations[todo.todo_id]) for todo in todos
    )


def read_todo_revision(*, user_id: str) -> int:
    """The user's TODO change counter, or 0 before the first TODO."""

    with todo_db_connection() as connection:
        row = connection.execute(
            "SELECT revision FROM todo_revisions WHERE user_id = ?", (user_id,)
        ).fetchone()
    return 0 if row is None else int(row[0])

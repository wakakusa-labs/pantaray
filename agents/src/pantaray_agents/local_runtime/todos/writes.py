"""Store the core's results: each write is one transaction over `todo_items`.

A write that changes nothing writes nothing, so a replayed request leaves the
row and the revision the live session follows as they were.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from typing import Final, Literal

from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.core import CheckSet, Rejected
from pantaray_agents.local_runtime.todos.db import (
    TODO_COLUMNS,
    TODO_SELECT,
    owns_project,
    read_todo_row,
    todo_db_connection,
    todo_from_row,
    todo_params,
)
from pantaray_agents.local_runtime.todos.model import (
    LocalDate,
    Todo,
    TodoAuthor,
    TodoDue,
    TodoId,
    WrittenOrigin,
)

_INSERT_SQL: Final = (
    f"INSERT INTO todo_items(user_id, client_todo_id, {', '.join(TODO_COLUMNS)}) "
    f"VALUES (:user_id, :client_todo_id, {', '.join(f':{c}' for c in TODO_COLUMNS)})"
)
_UPDATE_SQL: Final = (
    "UPDATE todo_items SET "
    + ", ".join(f"{column} = :{column}" for column in TODO_COLUMNS[1:])
    + " WHERE user_id = :user_id AND todo_id = :todo_id"
)

type CreateItemRejection = Literal["project_not_found", "client_todo_conflict"]


def create_todo_item(
    *,
    user_id: str,
    client_todo_id: str,
    project_id: str | None,
    text: str,
    week_of: LocalDate,
    due: TodoDue | None,
    clock: TodoClock,
) -> Todo | Rejected[core.CreateRejection | CreateItemRejection]:
    """The user's new TODO; the same `client_todo_id` again returns it."""

    created = core.create_todo(
        todo_id=str(uuid.uuid4()),
        text=text,
        author="user",
        origin=WrittenOrigin(),
        project_id=project_id,
        week_of=week_of,
        due=due,
        now=clock.now,
    )
    if isinstance(created, Rejected):
        return created
    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        row = connection.execute(
            f"SELECT {TODO_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.client_todo_id = ?",
            (user_id, client_todo_id),
        ).fetchone()
        if row is not None:
            existing = todo_from_row(row)
            # Compared as stored now: a replay that arrives after the TODO's
            # project or due changed is refused rather than undoing the change.
            if _request(existing) != _request(created):
                return Rejected("client_todo_conflict")
            return existing
        if project_id is not None and not owns_project(
            connection, user_id=user_id, project_id=project_id
        ):
            return Rejected("project_not_found")
        ensure_user_row(connection, user_id=user_id, timestamp=clock.now)
        connection.execute(
            _INSERT_SQL,
            {
                **todo_params(created),
                "user_id": user_id,
                "client_todo_id": client_todo_id,
            },
        )
    return created


def set_todo_check(
    *,
    user_id: str,
    todo_id: TodoId,
    by: TodoAuthor,
    checked: bool,
    clock: TodoClock,
) -> CheckSet | Rejected[core.CheckRejection] | None:
    """Check or uncheck; a weekly TODO's next instance is stored at most once."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        current = read_todo_row(connection, user_id=user_id, todo_id=todo_id)
        if current is None:
            return None
        result = core.set_check(
            current, by=by, checked=checked, now=clock.now, zone=clock.zone
        )
        if isinstance(result, Rejected):
            return result
        if result.todo != current:
            _update(connection, user_id=user_id, todo=result.todo)
        if result.next_todo is not None:
            connection.execute(
                _INSERT_SQL + " ON CONFLICT(todo_id) DO NOTHING",
                {
                    **todo_params(result.next_todo),
                    "user_id": user_id,
                    "client_todo_id": None,
                },
            )
    return result


def update_todo[R: str](
    *, user_id: str, todo_id: TodoId, step: Callable[[Todo], Todo | Rejected[R]]
) -> Todo | Rejected[R] | None:
    """Apply one core transition to the stored TODO; None when there is none.

    `step` is a core operation with its arguments bound, such as
    ``lambda todo: core.restore(todo, now=clock.now)``.
    """

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        current = read_todo_row(connection, user_id=user_id, todo_id=todo_id)
        if current is None:
            return None
        result = step(current)
        if isinstance(result, Todo) and result != current:
            _update(connection, user_id=user_id, todo=result)
    return result


def delete_purged_todo(*, user_id: str, todo: Todo) -> None:
    """Remove the row, unless its deletion changed since it was read."""

    assert todo.deletion is not None
    with todo_db_connection() as connection, immediate_transaction(connection):
        connection.execute(
            "DELETE FROM todo_items WHERE user_id = ? AND todo_id = ? AND deleted_at = ?",
            (user_id, todo.todo_id, todo.deletion.at),
        )


def _request(todo: Todo) -> tuple[str, str, str | None, TodoDue | None]:
    return (todo.text, todo.week_start, todo.project_id, todo.due)


def _update(connection: sqlite3.Connection, *, user_id: str, todo: Todo) -> None:
    connection.execute(_UPDATE_SQL, {**todo_params(todo), "user_id": user_id})

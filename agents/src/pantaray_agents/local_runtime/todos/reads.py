"""Read stored TODOs, each list with the conversations that go with its TODOs."""

from __future__ import annotations

from dataclasses import dataclass

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
from pantaray_agents.local_runtime.todos.week import (
    effective_week,
    shift_days,
    week_start_of,
)


@dataclass(frozen=True, slots=True)
class TodoEntry:
    todo: Todo
    conversation: TodoConversation


@dataclass(frozen=True, slots=True)
class WeekTodo:
    todo: Todo
    conversation: TodoConversation
    carried: bool  # an open TODO from an earlier week, shown in this one


@dataclass(frozen=True, slots=True)
class TodoWeek:
    today: LocalDate
    current_week_start: LocalDate
    week_start: LocalDate
    items: tuple[WeekTodo, ...]


def read_todo(*, user_id: str, todo_id: TodoId) -> TodoEntry | None:
    with todo_db_connection() as connection:
        todo = read_todo_row(connection, user_id=user_id, todo_id=todo_id)
        if todo is None:
            return None
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=(todo,)
        )
    return TodoEntry(todo=todo, conversation=conversations[todo.todo_id])


def read_todo_week(
    *,
    user_id: str,
    scope: TodoScope,
    project: TodoProjectFilter,
    week_of: LocalDate | None,
    clock: TodoClock,
) -> TodoWeek:
    """The TODOs one week's list shows; `week_of` None is the current week."""

    current = clock.current_week_start
    week = current if week_of is None else week_start_of(week_of)
    scope_sql, scope_params = todo_scope_sql(scope)
    project_sql, project_params = todo_project_sql(project)
    # Design limit: every open TODO from earlier weeks is read, as the current
    # week shows them all; bound the read when open TODOs exceed 1,000.
    sql = (
        f"SELECT {TODO_SELECT} FROM todo_items AS todo "
        "WHERE todo.user_id = :user_id AND todo.deleted_at IS NULL "
        "AND todo.week_start <= :week AND (todo.checked_at IS NULL "
        # A check on a local date in the week is no earlier in UTC than the day
        # before it, and one checked before its own week began stays there; the
        # exact week comes from effective_week below.
        "OR todo.checked_at >= :checked_from OR todo.week_start = :week) "
        f"AND {scope_sql} AND {project_sql} "
        "ORDER BY todo.week_start, todo.created_at, todo.todo_id"
    )
    params = {
        "user_id": user_id,
        "week": week,
        "checked_from": shift_days(week, -1),
        **scope_params,
        **project_params,
    }
    with todo_db_connection() as connection:
        shown = [
            (todo, placement.carried)
            for todo in map(todo_from_row, connection.execute(sql, params))
            if (placement := effective_week(todo, current, clock.zone)) is not None
            and placement.week_start == week
        ]
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=[todo for todo, _ in shown]
        )
    return TodoWeek(
        today=clock.today,
        current_week_start=current,
        week_start=week,
        items=tuple(
            WeekTodo(
                todo=todo, conversation=conversations[todo.todo_id], carried=carried
            )
            for todo, carried in shown
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


def read_pickup_candidates(
    *, user_id: str, current_week_start: LocalDate
) -> tuple[TodoEntry, ...]:
    """Open TODOs whose week has begun, with their conversations.

    A superset of what the pickup starts: `pickup_target` decides.
    """

    # Design limit: every open TODO is read on each pass, those whose Action
    # already runs too, and the scan walks the user's TODOs up to this week; add
    # a partial index on open TODOs when a pass exceeds 50 ms.
    with todo_db_connection() as connection:
        rows = connection.execute(
            f"SELECT {TODO_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.week_start <= ? "
            "AND todo.checked_at IS NULL AND todo.deleted_at IS NULL "
            "ORDER BY todo.created_at, todo.todo_id",
            (user_id, current_week_start),
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

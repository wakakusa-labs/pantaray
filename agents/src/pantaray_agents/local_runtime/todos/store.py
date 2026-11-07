"""Store the core's results: each write is one transaction over `todo_items`.

A write that changes nothing writes nothing, so a replayed request leaves the
row and the revision the live session follows as they were. The clock and the
user's zone are read here, by `read_todo_clock`, and handed to the core.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Final, Literal, assert_never, cast
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.conversation import (
    resolve_todo_conversations,
)
from pantaray_agents.local_runtime.todos.core import CheckSet, Rejected
from pantaray_agents.local_runtime.todos.model import (
    AiReport,
    DeletionRequest,
    LocalDate,
    RecurrenceOrigin,
    SuggestionOrigin,
    Todo,
    TodoAuthor,
    TodoCheck,
    TodoConversation,
    TodoDeletion,
    TodoDue,
    TodoId,
    TodoOrigin,
    WrittenOrigin,
)
from pantaray_agents.local_runtime.todos.week import (
    effective_week,
    local_date_at,
    shift_days,
    week_start_of,
)
from pantaray_agents.utils.local_time import local_zone_name

_COLUMNS: Final = tuple(
    """todo_id project_id text author origin_kind suggestion_id previous_todo_id
    week_start due_date due_repeat checked_by checked_at report_kind report_note
    report_run_id report_at deletion_request_id deletion_request_reason
    deletion_requested_at deletion_request_state deleted_by deleted_at
    created_at""".split()
)
_SELECT: Final = ", ".join(f"todo.{column}" for column in _COLUMNS)
_INSERT_SQL: Final = (
    f"INSERT INTO todo_items(user_id, client_todo_id, {', '.join(_COLUMNS)}) "
    f"VALUES (:user_id, :client_todo_id, {', '.join(f':{c}' for c in _COLUMNS)})"
)
_UPDATE_SQL: Final = (
    "UPDATE todo_items SET "
    + ", ".join(f"{column} = :{column}" for column in _COLUMNS[1:])
    + " WHERE user_id = :user_id AND todo_id = :todo_id"
)


class UnknownTimeZoneError(RuntimeError):
    """The OS zone has no IANA name, so local weeks cannot be computed."""


@dataclass(frozen=True, slots=True)
class TodoClock:
    now: str
    zone: ZoneInfo

    @property
    def today(self) -> LocalDate:
        return local_date_at(self.now, self.zone)

    @property
    def current_week_start(self) -> LocalDate:
        return week_start_of(self.today)


def read_todo_clock() -> TodoClock:
    """The only place the TODO list reads the clock and the user's zone."""

    zone_name = local_zone_name()
    if zone_name is None:
        raise UnknownTimeZoneError("the OS time zone has no IANA name")
    return TodoClock(now=now_utc_iso(), zone=ZoneInfo(zone_name))


@dataclass(frozen=True, slots=True)
class AllScope:
    kind: Literal["all"] = "all"


@dataclass(frozen=True, slots=True)
class OrganizationScope:
    organization_id: str
    kind: Literal["organization"] = "organization"


@dataclass(frozen=True, slots=True)
class NoOrganizationScope:
    kind: Literal["no_organization"] = "no_organization"


type TodoScope = AllScope | OrganizationScope | NoOrganizationScope


@dataclass(frozen=True, slots=True)
class AnyProject:
    kind: Literal["any"] = "any"


@dataclass(frozen=True, slots=True)
class OneProject:
    project_id: str
    kind: Literal["project"] = "project"


@dataclass(frozen=True, slots=True)
class NoProject:
    kind: Literal["no_project"] = "no_project"


type TodoProjectFilter = AnyProject | OneProject | NoProject


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
            f"SELECT {_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.client_todo_id = ?",
            (user_id, client_todo_id),
        ).fetchone()
        if row is not None:
            existing = _todo(row)
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
            {**_params(created), "user_id": user_id, "client_todo_id": client_todo_id},
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
        current = _read(connection, user_id=user_id, todo_id=todo_id)
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
                    **_params(result.next_todo),
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
        current = _read(connection, user_id=user_id, todo_id=todo_id)
        if current is None:
            return None
        result = step(current)
        if isinstance(result, Todo) and result != current:
            _update(connection, user_id=user_id, todo=result)
    return result


def read_todo(*, user_id: str, todo_id: TodoId) -> TodoEntry | None:
    with todo_db_connection() as connection:
        todo = _read(connection, user_id=user_id, todo_id=todo_id)
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
        f"SELECT {_SELECT} FROM todo_items AS todo "
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
            for todo in map(_todo, connection.execute(sql, params))
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
            f"SELECT {_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = :user_id AND todo.deleted_at IS NOT NULL "
            f"AND {scope_sql} ORDER BY todo.deleted_at DESC, todo.todo_id",
            {"user_id": user_id, **scope_params},
        ).fetchall()
    return tuple(
        todo for todo in map(_todo, rows) if not core.purge_due(todo, now=clock.now)
    )


def read_purge_due_todos(*, user_id: str, now: str) -> tuple[TodoEntry, ...]:
    """Deleted TODOs past retention, with the conversations that go with them."""

    with todo_db_connection() as connection:
        rows = connection.execute(
            f"SELECT {_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.deleted_at IS NOT NULL",
            (user_id,),
        ).fetchall()
        due = [todo for todo in map(_todo, rows) if core.purge_due(todo, now=now)]
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=due
        )
    return tuple(
        TodoEntry(todo=todo, conversation=conversations[todo.todo_id]) for todo in due
    )


def delete_purged_todo(*, user_id: str, todo: Todo) -> None:
    """Remove the row, unless its deletion changed since it was read."""

    assert todo.deletion is not None
    with todo_db_connection() as connection, immediate_transaction(connection):
        connection.execute(
            "DELETE FROM todo_items WHERE user_id = ? AND todo_id = ? AND deleted_at = ?",
            (user_id, todo.todo_id, todo.deletion.at),
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
            f"SELECT {_SELECT} FROM todo_items AS todo "
            "WHERE todo.user_id = ? AND todo.week_start <= ? "
            "AND todo.checked_at IS NULL AND todo.deleted_at IS NULL "
            "ORDER BY todo.created_at, todo.todo_id",
            (user_id, current_week_start),
        ).fetchall()
        todos = [_todo(row) for row in rows]
        conversations = resolve_todo_conversations(
            connection, user_id=user_id, todos=todos
        )
    return tuple(
        TodoEntry(todo=todo, conversation=conversations[todo.todo_id]) for todo in todos
    )


# The pickup opens a TODO's Action with a message whose id derives from the TODO
# id. SQLite cannot derive that id, but the same first message carries the
# TODO id in its hand-off, which only the pickup writes. A Suggestion's TODO is
# also served by the Action that accepted or replied to that Suggestion. Each
# branch is a lookup by key.
_HANDOFF_TODO_ID_SQL: Final = """(
  SELECT json_extract(step.user_message_json, '$.todo_handoff.todo_id')
  FROM agent_actions AS action
  JOIN agent_action_steps AS step
    ON step.user_id = action.user_id AND step.action_id = action.action_id
   AND step.user_message_id = action.initial_user_message_id
  WHERE action.user_id = :user_id AND action.action_id = :action_id)"""
_SERVED_TODO_SQL: Final = f"""
SELECT COALESCE(
  (SELECT todo_id FROM todo_items
   WHERE user_id = :user_id AND todo_id = {_HANDOFF_TODO_ID_SQL}),
  (SELECT todo.todo_id FROM agent_actions AS action
   JOIN todo_items AS todo
     ON todo.user_id = action.user_id AND todo.suggestion_id = action.suggestion_id
   WHERE action.user_id = :user_id AND action.action_id = :action_id),
  (SELECT todo.todo_id FROM agent_action_steps AS step
   JOIN todo_items AS todo
     ON todo.user_id = step.user_id AND todo.suggestion_id = step.source_suggestion_id
   WHERE step.user_id = :user_id AND step.action_id = :action_id)
)
"""
# The same links seen from a conversation being deleted, which already knows
# every Suggestion it holds.
_OPEN_TODO_OF_CONVERSATION_SQL: Final = f"""
SELECT EXISTS (SELECT 1 FROM todo_items
               WHERE user_id = :user_id AND deleted_at IS NULL
                 AND todo_id = {_HANDOFF_TODO_ID_SQL})
    OR EXISTS (SELECT 1 FROM todo_items
               WHERE user_id = :user_id AND deleted_at IS NULL
                 AND suggestion_id IN (SELECT value FROM json_each(:suggestion_ids)))
"""


def read_todo_for_action(*, user_id: str, action_id: str) -> TodoId | None:
    """The TODO whose conversation is this Action, or None for any other Action."""

    with todo_db_connection() as connection:
        row = connection.execute(
            _SERVED_TODO_SQL, {"user_id": user_id, "action_id": action_id}
        ).fetchone()
    return None if row[0] is None else str(row[0])


def conversation_has_open_todo(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str | None,
    suggestion_ids: Sequence[str],
) -> bool:
    """Whether a TODO that is not deleted has this conversation as its own.

    Read in the caller's transaction. Such a conversation goes only with its
    TODO: deleted alone, the pickup would find the TODO unstarted and run it
    again.
    """

    row = connection.execute(
        _OPEN_TODO_OF_CONVERSATION_SQL,
        {
            "user_id": user_id,
            "action_id": action_id,
            "suggestion_ids": json.dumps(list(suggestion_ids)),
        },
    ).fetchone()
    return bool(row[0])


def read_todo_revision(*, user_id: str) -> int:
    """The user's TODO change counter, or 0 before the first TODO."""

    with todo_db_connection() as connection:
        row = connection.execute(
            "SELECT revision FROM todo_revisions WHERE user_id = ?", (user_id,)
        ).fetchone()
    return 0 if row is None else int(row[0])


def todo_scope_sql(scope: TodoScope) -> tuple[str, dict[str, str]]:
    """A condition on `todo.project_id`; the query binds `:user_id`."""

    match scope:
        case AllScope():
            return "1", {}
        case OrganizationScope(organization_id=organization_id):
            return (
                "todo.project_id IN (SELECT project_id "
                "FROM workspace_project_organizations "
                "WHERE user_id = :user_id AND organization_id = :organization_id)",
                {"organization_id": organization_id},
            )
        case NoOrganizationScope():
            # A TODO without a project has no organization either.
            return (
                "NOT EXISTS (SELECT 1 FROM workspace_project_organizations AS link "
                "WHERE link.user_id = :user_id AND link.project_id = todo.project_id)",
                {},
            )
        case _:
            assert_never(scope)


def todo_project_sql(project: TodoProjectFilter) -> tuple[str, dict[str, str]]:
    match project:
        case AnyProject():
            return "1", {}
        case OneProject(project_id=project_id):
            return "todo.project_id = :project_id", {"project_id": project_id}
        case NoProject():
            return "todo.project_id IS NULL", {}
        case _:
            assert_never(project)


def owns_project(
    connection: sqlite3.Connection, *, user_id: str, project_id: str
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM workspace_projects WHERE user_id = ? AND project_id = ?",
        (user_id, project_id),
    ).fetchone()
    return row is not None


def _request(todo: Todo) -> tuple[str, str, str | None, TodoDue | None]:
    return (todo.text, todo.week_start, todo.project_id, todo.due)


def _read(
    connection: sqlite3.Connection, *, user_id: str, todo_id: TodoId
) -> Todo | None:
    row = connection.execute(
        f"SELECT {_SELECT} FROM todo_items AS todo "
        "WHERE todo.user_id = ? AND todo.todo_id = ?",
        (user_id, todo_id),
    ).fetchone()
    return None if row is None else _todo(row)


def _update(connection: sqlite3.Connection, *, user_id: str, todo: Todo) -> None:
    connection.execute(_UPDATE_SQL, {**_params(todo), "user_id": user_id})


def _params(todo: Todo) -> dict[str, str | None]:
    origin = todo.origin
    request = todo.deletion_request
    return {
        "todo_id": todo.todo_id,
        "project_id": todo.project_id,
        "text": todo.text,
        "author": todo.author,
        "origin_kind": origin.kind,
        "suggestion_id": (
            origin.suggestion_id if isinstance(origin, SuggestionOrigin) else None
        ),
        "previous_todo_id": (
            origin.previous_todo_id if isinstance(origin, RecurrenceOrigin) else None
        ),
        "week_start": todo.week_start,
        "due_date": None if todo.due is None else todo.due.date,
        "due_repeat": None if todo.due is None else todo.due.repeat,
        "checked_by": None if todo.check is None else todo.check.by,
        "checked_at": None if todo.check is None else todo.check.at,
        "report_kind": None if todo.ai_report is None else todo.ai_report.kind,
        "report_note": None if todo.ai_report is None else todo.ai_report.note,
        "report_run_id": None if todo.ai_report is None else todo.ai_report.run_id,
        "report_at": None if todo.ai_report is None else todo.ai_report.at,
        "deletion_request_id": None if request is None else request.request_id,
        "deletion_request_reason": None if request is None else request.reason,
        "deletion_requested_at": None if request is None else request.requested_at,
        "deletion_request_state": None if request is None else request.state,
        "deleted_by": None if todo.deletion is None else todo.deletion.by,
        "deleted_at": None if todo.deletion is None else todo.deletion.at,
        "created_at": todo.created_at,
    }


def _todo(row: sqlite3.Row) -> Todo:
    return Todo(
        todo_id=row["todo_id"],
        project_id=row["project_id"],
        text=row["text"],
        author=row["author"],
        origin=_origin(row),
        week_start=row["week_start"],
        due=(
            None
            if row["due_date"] is None
            else TodoDue(date=row["due_date"], repeat=row["due_repeat"])
        ),
        check=(
            None
            if row["checked_by"] is None
            else TodoCheck(by=row["checked_by"], at=row["checked_at"])
        ),
        ai_report=(
            None
            if row["report_kind"] is None
            else AiReport(
                kind=row["report_kind"],
                note=row["report_note"],
                run_id=row["report_run_id"],
                at=row["report_at"],
            )
        ),
        deletion_request=(
            None
            if row["deletion_request_id"] is None
            else DeletionRequest(
                request_id=row["deletion_request_id"],
                reason=row["deletion_request_reason"],
                requested_at=row["deletion_requested_at"],
                state=row["deletion_request_state"],
            )
        ),
        deletion=(
            None
            if row["deleted_by"] is None
            else TodoDeletion(by=row["deleted_by"], at=row["deleted_at"])
        ),
        created_at=row["created_at"],
    )


def _origin(row: sqlite3.Row) -> TodoOrigin:
    kind = cast("Literal['written', 'suggestion', 'recurrence']", row["origin_kind"])
    if kind == "suggestion":
        return SuggestionOrigin(suggestion_id=row["suggestion_id"])
    if kind == "recurrence":
        return RecurrenceOrigin(previous_todo_id=row["previous_todo_id"])
    return WrittenOrigin()


@contextmanager
def todo_db_connection() -> Iterator[sqlite3.Connection]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        yield connection

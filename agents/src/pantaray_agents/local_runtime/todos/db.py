"""The connection, `todo_items` row codec and list filters the TODO stores share."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Final, Literal, assert_never, cast

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.todos.model import (
    AiReport,
    DeletionRequest,
    RecurrenceOrigin,
    SuggestionOrigin,
    Todo,
    TodoCheck,
    TodoDeletion,
    TodoDue,
    TodoId,
    TodoOrigin,
    WrittenOrigin,
)

TODO_COLUMNS: Final = tuple(
    """todo_id project_id text author origin_kind suggestion_id previous_todo_id
    week_start due_date due_repeat checked_by checked_at report_kind report_note
    report_run_id report_at deletion_request_id deletion_request_reason
    deletion_requested_at deletion_request_state deleted_by deleted_at
    created_at""".split()
)
TODO_SELECT: Final = ", ".join(f"todo.{column}" for column in TODO_COLUMNS)


@contextmanager
def todo_db_connection() -> Iterator[sqlite3.Connection]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        yield connection


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


def read_todo_row(
    connection: sqlite3.Connection, *, user_id: str, todo_id: TodoId
) -> Todo | None:
    row = connection.execute(
        f"SELECT {TODO_SELECT} FROM todo_items AS todo "
        "WHERE todo.user_id = ? AND todo.todo_id = ?",
        (user_id, todo_id),
    ).fetchone()
    return None if row is None else todo_from_row(row)


def todo_params(todo: Todo) -> dict[str, str | None]:
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


def todo_from_row(row: sqlite3.Row) -> Todo:
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

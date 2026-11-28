"""The connection, `todo_items` row codec and list filters the TODO stores share."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Final, Literal, assert_never, cast

from pydantic import BaseModel, ConfigDict

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.todos.model import (
    AiReport,
    DeletionRequest,
    HookOrigin,
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
from pantaray_agents.schema.agent.action_message import FileAttachmentInput
from pantaray_agents.schema.agent.image import ImageInput

TODO_COLUMNS: Final = tuple(
    """todo_id project_id organization_id text author origin_kind suggestion_id
    previous_todo_id hook_origin_action_id starts_on due_date due_repeat
    checked_by checked_at report_kind report_note report_run_id report_at
    deletion_request_id deletion_request_reason deletion_requested_at
    deletion_request_state deleted_by deleted_at created_at attachments_json deferred_run_id""".split()
)


class _StoredAttachments(BaseModel):
    """`todo_items.attachments_json`: a written TODO's attachments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    files: tuple[FileAttachmentInput, ...]
    images: tuple[ImageInput, ...]


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


def project_scope_sql(scope: TodoScope) -> tuple[str, dict[str, str]]:
    """A condition on `todo.project_id` alone, for rows that belong to a
    project; the query binds `:user_id`."""

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
            return (
                "NOT EXISTS (SELECT 1 FROM workspace_project_organizations AS link "
                "WHERE link.user_id = :user_id AND link.project_id = todo.project_id)",
                {},
            )
        case _:
            assert_never(scope)


def todo_scope_sql(scope: TodoScope) -> tuple[str, dict[str, str]]:
    """A condition on `todo.project_id` and `todo.organization_id`: a TODO is
    in its project's organization, or in its own while it has no project."""

    in_projects, params = project_scope_sql(scope)
    match scope:
        case AllScope():
            return in_projects, params
        case OrganizationScope():
            return f"({in_projects} OR todo.organization_id = :organization_id)", params
        case NoOrganizationScope():
            return f"({in_projects} AND todo.organization_id IS NULL)", params
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


def owns_organization(
    connection: sqlite3.Connection, *, user_id: str, organization_id: str
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM workspace_organizations WHERE user_id = ? AND organization_id = ?",
        (user_id, organization_id),
    ).fetchone()
    return row is not None


def project_id_named(
    connection: sqlite3.Connection, *, user_id: str, display_name: str
) -> str | None:
    """The user's project with exactly this name; names are unique per user."""

    row = connection.execute(
        "SELECT project_id FROM workspace_projects WHERE user_id = ? AND display_name = ?",
        (user_id, display_name),
    ).fetchone()
    return None if row is None else str(row[0])


def count_todo_change(connection: sqlite3.Connection, *, user_id: str) -> None:
    """Move the counter the live session follows, in the caller's transaction.

    The `todo_items` triggers count every row change; a TODO whose Action
    starts changes what it shows through the Action alone.
    """

    connection.execute(
        "INSERT INTO todo_revisions(user_id, revision) VALUES (?, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1",
        (user_id,),
    )


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
        "organization_id": todo.organization_id,
        "text": todo.text,
        "author": todo.author,
        "origin_kind": origin.kind,
        "suggestion_id": (
            origin.suggestion_id if isinstance(origin, SuggestionOrigin) else None
        ),
        "previous_todo_id": (
            origin.previous_todo_id if isinstance(origin, RecurrenceOrigin) else None
        ),
        "hook_origin_action_id": (
            origin.origin_action_id if isinstance(origin, HookOrigin) else None
        ),
        "starts_on": todo.starts_on,
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
        "attachments_json": (
            _StoredAttachments(
                files=origin.files, images=origin.images
            ).model_dump_json()
            if isinstance(origin, WrittenOrigin) and (origin.files or origin.images)
            else None
        ),
        "deferred_run_id": todo.deferred_run_id,
    }


def todo_from_row(row: sqlite3.Row) -> Todo:
    return Todo(
        todo_id=row["todo_id"],
        project_id=row["project_id"],
        organization_id=row["organization_id"],
        text=row["text"],
        author=row["author"],
        origin=_origin(row),
        starts_on=row["starts_on"],
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
        deferred_run_id=row["deferred_run_id"],
    )


def _origin(row: sqlite3.Row) -> TodoOrigin:
    kind = cast(
        "Literal['written', 'suggestion', 'recurrence', 'hook']", row["origin_kind"]
    )
    if kind == "suggestion":
        return SuggestionOrigin(suggestion_id=row["suggestion_id"])
    if kind == "recurrence":
        return RecurrenceOrigin(previous_todo_id=row["previous_todo_id"])
    if kind == "hook":
        return HookOrigin(origin_action_id=row["hook_origin_action_id"])
    if row["attachments_json"] is None:
        return WrittenOrigin()
    stored = _StoredAttachments.model_validate_json(row["attachments_json"])
    return WrittenOrigin(files=stored.files, images=stored.images)

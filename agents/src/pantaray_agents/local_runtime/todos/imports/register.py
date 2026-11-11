"""Register a TODO import: everything its draft names, written in one transaction.

The draft is resolved again against the workspace as it is now, so a change
made since the last write is refused or reused rather than written over. A
repeated registration answers what the first one created.
"""

from __future__ import annotations

import sqlite3
import uuid

from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.db import count_todo_change, todo_db_connection
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    DraftTodo,
    ExistingOrganization,
    ExistingProject,
    ImportClosed,
    NewOrganization,
    NewProject,
    ResolvedGroup,
    StaleRevision,
    TodoImportResult,
)
from pantaray_agents.local_runtime.todos.imports.store import (
    import_row,
    import_view,
    load_workspace,
)
from pantaray_agents.local_runtime.todos.model import Todo, TodoDue, WrittenOrigin
from pantaray_agents.local_runtime.todos.writes import insert_todo
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    insert_workspace_organization,
    insert_workspace_project,
)


def register_todo_import(
    *, user_id: str, import_id: str, expected_revision: int, clock: TodoClock
) -> TodoImportResult | StaleRevision | ImportClosed | DraftIssue | None:
    """Register the draft at `expected_revision`, the one the user confirmed."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        row = import_row(connection, user_id=user_id, import_id=import_id)
        if row is None:
            return None
        if row["state"] == "registered":
            return TodoImportResult.model_validate_json(row["result_json"])
        if row["state"] == "canceled":
            return ImportClosed("canceled")
        view = import_view(row, load_workspace(connection, user_id=user_id))
        # A draft rewritten after the user looked is not what they confirmed.
        if view.revision != expected_revision:
            return StaleRevision(view)
        if view.issue is not None:
            return view.issue
        ensure_user_row(connection, user_id=user_id, timestamp=clock.now)
        result = _write_groups(
            connection, user_id=user_id, groups=view.groups, now=clock.now
        )
        connection.execute(
            "UPDATE todo_import_drafts SET state = 'registered', result_json = ?, "
            "updated_at = ? WHERE user_id = ? AND import_id = ?",
            (result.model_dump_json(), clock.now, user_id, import_id),
        )
        count_todo_change(connection, user_id=user_id)
    return result


def _write_groups(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    groups: tuple[ResolvedGroup, ...],
    now: str,
) -> TodoImportResult:
    # New names to the rows made for them: two groups may name one new project.
    organizations: dict[str, str] = {}
    projects: dict[str, str] = {}
    todo_ids: list[str] = []
    skipped = 0
    for group in groups:
        organization_id: str | None = None
        match group.organization:
            case ExistingOrganization(organization_id=existing_id):
                organization_id = existing_id
            case NewOrganization(name=name):
                if name not in organizations:
                    organizations[name] = insert_workspace_organization(
                        connection,
                        user_id=user_id,
                        display_name=name,
                        icon_data_url=None,
                        now=now,
                    ).organization_id
                organization_id = organizations[name]
        project_id: str | None = None
        match group.project:
            case ExistingProject(project_id=existing_id):
                project_id = existing_id
            case NewProject(name=name):
                if name not in projects:
                    projects[name] = insert_workspace_project(
                        connection,
                        user_id=user_id,
                        display_name=name,
                        organization_ids=(
                            () if organization_id is None else (organization_id,)
                        ),
                        now=now,
                    ).project_id
                project_id = projects[name]
        for line in group.todos:
            if line.duplicate and not line.todo.keep_duplicate:
                skipped += 1
                continue
            todo = _todo(line.todo, project_id=project_id, now=now)
            insert_todo(connection, user_id=user_id, todo=todo, client_todo_id=None)
            todo_ids.append(todo.todo_id)
    return TodoImportResult(
        organization_ids=tuple(organizations.values()),
        project_ids=tuple(projects.values()),
        todo_ids=tuple(todo_ids),
        skipped_duplicates=skipped,
    )


def _todo(draft: DraftTodo, *, project_id: str | None, now: str) -> Todo:
    created = core.create_todo(
        todo_id=str(uuid.uuid4()),
        text=draft.text,
        author="user",
        origin=WrittenOrigin(),
        project_id=project_id,
        starts_on=None if draft.starts_on is None else draft.starts_on.isoformat(),
        due=None if draft.due is None else TodoDue(draft.due.isoformat(), "none"),
        now=now,
    )
    assert isinstance(created, Todo)  # `resolve_draft` checked the text.
    return created

"""Store a TODO import's draft: each write is one transaction that replaces the
whole draft at the revision the writer read, and moves the TODO change counter
so the screen reads the draft again.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import cast

from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.db import (
    AllScope,
    NoOrganizationScope,
    OrganizationScope,
    TodoScope,
    count_todo_change,
    todo_db_connection,
)
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    ExistingProject,
    ImportClosed,
    ImportState,
    StaleRevision,
    TodoImportDraft,
    TodoImportResult,
    TodoImportView,
)
from pantaray_agents.local_runtime.todos.imports.resolution import (
    Workspace,
    resolve_draft,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    load_workspace_settings_in_connection,
)


def open_todo_import(
    *, user_id: str, scope: TodoScope, clock: TodoClock
) -> TodoImportView | DraftIssue:
    """A new import with an empty draft, at revision 1."""

    verify_current_owner(user_id)
    import_id = str(uuid.uuid4())
    with todo_db_connection() as connection, immediate_transaction(connection):
        workspace = load_workspace(connection, user_id=user_id)
        _, issue = resolve_draft(TodoImportDraft(), scope=scope, workspace=workspace)
        if issue is not None:
            return issue
        ensure_user_row(connection, user_id=user_id, timestamp=clock.now)
        connection.execute(
            "INSERT INTO todo_import_drafts(import_id, user_id, scope_kind, "
            "organization_id, draft_json, revision, state, result_json, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, 1, 'open', NULL, ?, ?)",
            (
                import_id,
                user_id,
                scope.kind,
                scope.organization_id if isinstance(scope, OrganizationScope) else None,
                TodoImportDraft().model_dump_json(),
                clock.now,
                clock.now,
            ),
        )
        count_todo_change(connection, user_id=user_id)
        return _reread(connection, user_id, import_id, workspace)


def read_todo_import(*, user_id: str, import_id: str) -> TodoImportView | None:
    verify_current_owner(user_id)
    with todo_db_connection() as connection:
        row = import_row(connection, user_id=user_id, import_id=import_id)
        if row is None:
            return None
        return import_view(row, load_workspace(connection, user_id=user_id))


def write_todo_import_draft(
    *,
    user_id: str,
    import_id: str,
    draft: TodoImportDraft,
    expected_revision: int,
    clock: TodoClock,
) -> TodoImportView | StaleRevision | ImportClosed | DraftIssue | None:
    """Replace the whole draft; the one write path for every writer."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        row = import_row(connection, user_id=user_id, import_id=import_id)
        if row is None:
            return None
        state = _state(row)
        if state != "open":
            return ImportClosed(state)
        workspace = load_workspace(connection, user_id=user_id)
        if int(row["revision"]) != expected_revision:
            return StaleRevision(import_view(row, workspace))
        _, issue = resolve_draft(draft, scope=_scope(row), workspace=workspace)
        if issue is not None:
            return issue
        connection.execute(
            "UPDATE todo_import_drafts SET draft_json = ?, revision = revision + 1, "
            "updated_at = ? WHERE user_id = ? AND import_id = ?",
            (draft.model_dump_json(), clock.now, user_id, import_id),
        )
        count_todo_change(connection, user_id=user_id)
        return _reread(connection, user_id, import_id, workspace)


def cancel_todo_import(
    *, user_id: str, import_id: str, clock: TodoClock
) -> TodoImportView | ImportClosed | None:
    """Close the import without registering; canceling again answers the same."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        row = import_row(connection, user_id=user_id, import_id=import_id)
        if row is None:
            return None
        state = _state(row)
        if state == "registered":
            return ImportClosed(state)
        if state == "open":
            connection.execute(
                "UPDATE todo_import_drafts SET state = 'canceled', updated_at = ? "
                "WHERE user_id = ? AND import_id = ?",
                (clock.now, user_id, import_id),
            )
            count_todo_change(connection, user_id=user_id)
        return _reread(
            connection, user_id, import_id, load_workspace(connection, user_id=user_id)
        )


def import_row(
    connection: sqlite3.Connection, *, user_id: str, import_id: str
) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        connection.execute(
            "SELECT import_id, scope_kind, organization_id, draft_json, revision, "
            "state, result_json FROM todo_import_drafts "
            "WHERE user_id = ? AND import_id = ?",
            (user_id, import_id),
        ).fetchone(),
    )


def import_view(row: sqlite3.Row, workspace: Workspace) -> TodoImportView:
    scope = _scope(row)
    draft = TodoImportDraft.model_validate_json(row["draft_json"])
    groups, issue = resolve_draft(draft, scope=scope, workspace=workspace)
    return TodoImportView(
        import_id=row["import_id"],
        scope=scope,
        revision=int(row["revision"]),
        state=_state(row),
        draft=draft,
        groups=groups,
        issue=issue,
        result=(
            None
            if row["result_json"] is None
            else TodoImportResult.model_validate_json(row["result_json"])
        ),
    )


def load_workspace(connection: sqlite3.Connection, *, user_id: str) -> Workspace:
    settings = load_workspace_settings_in_connection(
        connection=connection, user_id=user_id
    )
    # Design limit: every open TODO's text is read to mark duplicates; read
    # only the draft's projects when a read exceeds 50 ms.
    open_todos = connection.execute(
        "SELECT project_id, text FROM todo_items "
        "WHERE user_id = ? AND checked_at IS NULL AND deleted_at IS NULL",
        (user_id,),
    ).fetchall()
    return Workspace(
        organization_ids={
            organization.display_name: organization.organization_id
            for organization in settings.organizations
        },
        projects={
            project.display_name: ExistingProject(
                project_id=project.project_id,
                name=project.display_name,
                organization_ids=frozenset(project.organization_ids),
            )
            for project in settings.projects
        },
        open_todos=frozenset((row[0], row[1]) for row in open_todos),
    )


def _reread(
    connection: sqlite3.Connection, user_id: str, import_id: str, workspace: Workspace
) -> TodoImportView:
    row = import_row(connection, user_id=user_id, import_id=import_id)
    assert row is not None  # Written in this transaction.
    return import_view(row, workspace)


def _state(row: sqlite3.Row) -> ImportState:
    return cast("ImportState", row["state"])


def _scope(row: sqlite3.Row) -> TodoScope:
    if row["organization_id"] is not None:
        return OrganizationScope(organization_id=row["organization_id"])
    return (
        NoOrganizationScope() if row["scope_kind"] == "no_organization" else AllScope()
    )

"""Store the core's results: each write is one transaction over `todo_items`,
except a Suggestion's TODO, which joins the Suggestion's save. Every row it
writes records `todo.saved` in that transaction, for the pickup. Deleting a
TODO deletes its conversation's hooks in the same transaction.

A write that changes nothing writes nothing, so a replayed request leaves the
row and the revision the live session follows as they were.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Final, Literal

from pantaray_agents.local_runtime.hooks.events import TodoSaved
from pantaray_agents.local_runtime.hooks.inbox import record_event
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
    require_staged_attachments,
)
from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import TodoClock
from pantaray_agents.local_runtime.todos.conversation import (
    resolve_todo_conversations,
)
from pantaray_agents.local_runtime.todos.core import CheckSet, Rejected
from pantaray_agents.local_runtime.todos.db import (
    TODO_COLUMNS,
    TODO_SELECT,
    owns_organization,
    owns_project,
    project_id_named,
    read_todo_row,
    todo_db_connection,
    todo_from_row,
    todo_params,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    AiReport,
    LocalDate,
    SuggestionOrigin,
    Todo,
    TodoAuthor,
    TodoDue,
    TodoId,
    TodoOrigin,
    WrittenOrigin,
)
from pantaray_agents.schema.agent.action_message import FileAttachmentInput
from pantaray_agents.schema.agent.image import ImageInput
from pantaray_agents.schema.agent.suggestion import SuggestionTodo
from pantaray_agents.security.storage_paths import validate_image_storage_path

_INSERT_SQL: Final = (
    f"INSERT INTO todo_items(user_id, client_todo_id, {', '.join(TODO_COLUMNS)}) "
    f"VALUES (:user_id, :client_todo_id, {', '.join(f':{c}' for c in TODO_COLUMNS)})"
)
_UPDATE_SQL: Final = (
    "UPDATE todo_items SET "
    + ", ".join(f"{column} = :{column}" for column in TODO_COLUMNS[1:])
    + " WHERE user_id = :user_id AND todo_id = :todo_id"
)

type CreateItemRejection = Literal[
    "project_not_found",
    "organization_not_found",
    "client_todo_conflict",
    "attachment_unavailable",
]

_NAMESPACE_SUGGESTION_TODO: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:suggestion_todo:v1"
)


def create_todo_item(
    *,
    user_id: str,
    client_todo_id: str,
    project_id: str | None,
    organization_id: str | None,
    text: str,
    starts_on: LocalDate | None,
    due: TodoDue | None,
    clock: TodoClock,
    files: tuple[FileAttachmentInput, ...] = (),
    images: tuple[ImageInput, ...] = (),
) -> Todo | Rejected[core.CreateRejection | CreateItemRejection]:
    """The user's new TODO; the same `client_todo_id` again returns it.

    `organization_id` is its own organization, so it is None with a project.
    `files` are staged for the user and `images` are in the user's scope; the
    TODO holds them until the pickup hands them to its Action.
    """

    created = core.create_todo(
        todo_id=str(uuid.uuid4()),
        text=text,
        author="user",
        origin=WrittenOrigin(files=files, images=images),
        project_id=project_id,
        organization_id=organization_id,
        starts_on=starts_on,
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
            # place, start or due changed is refused rather than undoing it.
            if _request(existing) != _request(created):
                return Rejected("client_todo_conflict")
            return existing
        if project_id is not None and not owns_project(
            connection, user_id=user_id, project_id=project_id
        ):
            return Rejected("project_not_found")
        if organization_id is not None and not owns_organization(
            connection, user_id=user_id, organization_id=organization_id
        ):
            return Rejected("organization_not_found")
        # Checked only for a new TODO: a replay finds its files already taken
        # by the Action the pickup started.
        if not _attachments_available(user_id, files, images):
            return Rejected("attachment_unavailable")
        ensure_user_row(connection, user_id=user_id, timestamp=clock.now)
        insert_todo(
            connection, user_id=user_id, todo=created, client_todo_id=client_todo_id
        )
    return created


def insert_todo(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    todo: Todo,
    client_todo_id: str | None,
) -> None:
    """Store a new TODO in the caller's transaction, whose project is the user's."""

    connection.execute(
        _INSERT_SQL,
        {**todo_params(todo), "user_id": user_id, "client_todo_id": client_todo_id},
    )
    _record_saved(connection, user_id=user_id)


def suggestion_todo_id(suggestion_id: str) -> TodoId:
    return str(uuid.uuid5(_NAMESPACE_SUGGESTION_TODO, suggestion_id))


def build_suggestion_todo(
    *, suggestion_id: str, todo: SuggestionTodo, clock: TodoClock
) -> Todo | Rejected[core.CreateRejection]:
    """The AI-written TODO a Suggestion becomes, before its project is known.

    It starts today, unchecked and without a due date, with the Suggestion's
    report on it unless it is open.
    """

    created = core.create_todo(
        todo_id=suggestion_todo_id(suggestion_id),
        text=todo.text,
        author="ai",
        origin=SuggestionOrigin(suggestion_id=suggestion_id),
        project_id=None,
        organization_id=None,
        starts_on=clock.today,
        due=None,
        now=clock.now,
    )
    if isinstance(created, Rejected):
        return created
    if todo.outcome == "open":
        return created
    assert todo.note is not None  # SuggestionTodo requires it.
    reported = core.report_progress(
        created,
        AiReport(kind=todo.outcome, note=todo.note, run_id=None, at=clock.now),
    )
    assert isinstance(reported, Todo)  # A new TODO is not deleted.
    return reported


def insert_suggestion_todo(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    todo: Todo,
    project_name: str | None,
) -> None:
    """Store `build_suggestion_todo`'s TODO in the Suggestion's save transaction.

    Its project is the user's project of exactly ``project_name``; any other
    name leaves it without one rather than guessing.
    """

    project_id = (
        None
        if project_name is None
        else project_id_named(connection, user_id=user_id, display_name=project_name)
    )
    insert_todo(
        connection,
        user_id=user_id,
        todo=replace(todo, project_id=project_id),
        client_todo_id=None,
    )


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
            # The check above records the save that covers this instance too.
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

    A transition that deletes the TODO also deletes the hooks asked for in
    its conversation and those that send to it, as deleting the conversation
    would; restoring the TODO does not bring them back.
    """

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        current = read_todo_row(connection, user_id=user_id, todo_id=todo_id)
        if current is None:
            return None
        result = step(current)
        if isinstance(result, Todo) and result != current:
            _update(connection, user_id=user_id, todo=result)
            if current.deletion is None and result.deletion is not None:
                _delete_conversation_hooks(connection, user_id=user_id, todo=result)
    return result


def set_todo_start(
    *, user_id: str, todo_id: TodoId, starts_on: LocalDate | None, today: LocalDate
) -> Todo | Rejected[core.StartRejection] | None:
    """The user's new start, judged against the conversation read in the same
    transaction, so the run it records is the latest one; None when there is
    no such TODO."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        current = read_todo_row(connection, user_id=user_id, todo_id=todo_id)
        if current is None:
            return None
        conversation = resolve_todo_conversations(
            connection, user_id=user_id, todos=(current,)
        )[todo_id]
        result = core.set_start(
            current, starts_on, conversation=conversation, today=today
        )
        if isinstance(result, Todo) and result != current:
            _update(connection, user_id=user_id, todo=result)
    return result


def release_todo_attachments(*, user_id: str, todo_id: TodoId) -> None:
    """Drop a written TODO's attachments once its Action has taken them."""

    update_todo(
        user_id=user_id,
        todo_id=todo_id,
        step=lambda todo: (
            replace(todo, origin=WrittenOrigin())
            if isinstance(todo.origin, WrittenOrigin)
            else todo
        ),
    )


def delete_purged_todo(*, user_id: str, todo: Todo) -> bool:
    """Remove the row, unless its deletion changed since it was read; whether
    it was removed."""

    assert todo.deletion is not None
    with todo_db_connection() as connection, immediate_transaction(connection):
        cursor = connection.execute(
            "DELETE FROM todo_items WHERE user_id = ? AND todo_id = ? AND deleted_at = ?",
            (user_id, todo.todo_id, todo.deletion.at),
        )
    return cursor.rowcount == 1


def _request(
    todo: Todo,
) -> tuple[str, str | None, str | None, str | None, TodoDue | None, TodoOrigin]:
    return (
        todo.text,
        todo.starts_on,
        todo.project_id,
        todo.organization_id,
        todo.due,
        todo.origin,
    )


def _attachments_available(
    user_id: str, files: tuple[FileAttachmentInput, ...], images: tuple[ImageInput, ...]
) -> bool:
    try:
        for image in images:
            validate_image_storage_path(
                user_id=user_id, storage_path=image.storage_path
            )
        require_staged_attachments(
            artifact_root=read_local_runtime_artifact_root(),
            user_id=user_id,
            files=files,
        )
    except (ValueError, ActionFileAttachmentUnavailableError):
        return False
    return True


def _delete_conversation_hooks(
    connection: sqlite3.Connection, *, user_id: str, todo: Todo
) -> None:
    """Delete the hooks whose origin or target is the TODO's conversation.

    Done when the TODO is deleted rather than when it is purged: until then
    they would keep sending turns to, or starting work from, a conversation
    the user deleted. Their state goes with the row, and their inbox with its
    trigger. A hook whose fire opened this TODO asked elsewhere, so it stays.
    """

    conversation = resolve_todo_conversations(
        connection, user_id=user_id, todos=(todo,)
    )[todo.todo_id]
    if not isinstance(conversation, ActionConversation):
        return
    connection.execute(
        "DELETE FROM user_hooks WHERE user_id = :user_id "
        "AND (origin_action_id = :action_id OR target_action_id = :action_id)",
        {"user_id": user_id, "action_id": conversation.action_id},
    )


def _update(connection: sqlite3.Connection, *, user_id: str, todo: Todo) -> None:
    connection.execute(_UPDATE_SQL, {**todo_params(todo), "user_id": user_id})
    _record_saved(connection, user_id=user_id)


def _record_saved(connection: sqlite3.Connection, *, user_id: str) -> None:
    record_event(
        connection, user_id=user_id, event=TodoSaved(), occurred_at=now_utc_iso()
    )

"""A TODO import over HTTP: start it, read and edit its draft, register or
cancel it. The user is the actor on every route; the import's Action writes
the draft through its own tool, held to the same validation.

A refusal is a `TodoImportRefused` body; none of them names a TODO's text.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    ActionFileAttachmentUnavailableError,
)
from pantaray_agents.local_runtime.runtime.action_message_models import (
    ActionMessageConflictError,
)
from pantaray_agents.local_runtime.todos.clock import read_todo_clock
from pantaray_agents.local_runtime.todos.imports.model import (
    DraftIssue,
    ImportClosed,
    StaleRevision,
    TodoImportView,
)
from pantaray_agents.local_runtime.todos.imports.register import register_todo_import
from pantaray_agents.local_runtime.todos.imports.start import (
    list_open_todo_imports,
    read_todo_import_action_id,
    start_todo_import,
)
from pantaray_agents.local_runtime.todos.imports.store import (
    cancel_todo_import,
    read_todo_import,
    write_todo_import_draft,
)
from pantaray_agents.schema.todo_imports import (
    OpenTodoImportView,
    TodoImportDraftRequest,
    TodoImportHttpView,
    TodoImportRefusalReason,
    TodoImportRefused,
    TodoImportRegisterRequest,
    TodoImportStartRequest,
    todo_import_http_view,
)
from pantaray_agents.schema.todos import TodoScopeQuery
from pantaray_agents.security.storage_paths import is_valid_image_storage_path

router = APIRouter(
    prefix="/v1/agents/users",
    tags=["TODO imports"],
    responses={
        code: {"model": TodoImportRefused}
        for code in (
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    },
)

_REFUSED_STATUS: Final[dict[str, int]] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "stale_revision": status.HTTP_409_CONFLICT,
    "registered": status.HTTP_409_CONFLICT,
    "canceled": status.HTTP_409_CONFLICT,
    "start_conflict": status.HTTP_409_CONFLICT,
}


def _require_path_user(user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )


def _refused(
    reason: TodoImportRefusalReason, issue: DraftIssue | None = None
) -> JSONResponse:
    body = TodoImportRefused(
        reason=reason,
        group_index=None if issue is None else issue.group_index,
        todo_index=None if issue is None else issue.todo_index,
    )
    return JSONResponse(
        # A draft issue, or a staged file the backend no longer has.
        status_code=_REFUSED_STATUS.get(reason, status.HTTP_422_UNPROCESSABLE_CONTENT),
        content=body.model_dump(mode="json"),
    )


def _view(user_id: str, view: TodoImportView) -> TodoImportHttpView:
    return todo_import_http_view(
        view,
        action_id=read_todo_import_action_id(user_id=user_id, import_id=view.import_id),
    )


def _written(
    user_id: str,
    written: TodoImportView | StaleRevision | ImportClosed | DraftIssue | None,
) -> TodoImportHttpView | JSONResponse:
    match written:
        case TodoImportView():
            return _view(user_id, written)
        case StaleRevision():
            return _refused("stale_revision")
        case ImportClosed(state=closed):
            return _refused(closed)
        case DraftIssue(reason=reason):
            return _refused(reason, written)
        case None:
            return _refused("not_found")


@router.post(
    "/{user_id}/todo-imports",
    response_model=TodoImportHttpView,
    description=(
        "Open the import and start the Action that drafts it from the text, "
        "files and images. Resending the same `import_id` returns the same "
        "import and Action; with different text, files or images, 409 "
        "`start_conflict`. An image outside the user's scope is 422 "
        "`images_invalid`, before anything opens."
    ),
)
def post_todo_import(
    user_id: str,
    body: TodoImportStartRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoImportHttpView | JSONResponse:
    _require_path_user(user_id, resolved_user_id)
    # The check `POST .../actions/messages` holds an image to.
    if not all(
        is_valid_image_storage_path(user_id=user_id, storage_path=image.storage_path)
        for image in body.images
    ):
        return _refused("images_invalid")
    try:
        started = start_todo_import(
            user_id=user_id,
            import_id=body.import_id,
            scope=body.todo_scope(),
            text=body.text,
            files=body.files,
            images=body.images,
            clock=read_todo_clock(),
        )
    except ActionFileAttachmentUnavailableError:
        return _refused("files_unavailable")
    except ActionMessageConflictError:
        return _refused("start_conflict")
    if isinstance(started, DraftIssue):
        return _refused(started.reason, started)
    return todo_import_http_view(started.view, action_id=started.action_id)


@router.get(
    "/{user_id}/todo-imports",
    response_model=tuple[OpenTodoImportView, ...],
    description=(
        "The imports neither registered nor canceled, newest first: those "
        "started in the scope, or every one for `all`."
    ),
)
def get_open_todo_imports(
    user_id: str,
    query: Annotated[TodoScopeQuery, Query()],
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> tuple[OpenTodoImportView, ...]:
    _require_path_user(user_id, resolved_user_id)
    return tuple(
        OpenTodoImportView(
            import_id=opened.import_id,
            request=opened.request,
            started_at=opened.started_at,
        )
        for opened in list_open_todo_imports(user_id=user_id, scope=query.todo_scope())
    )


@router.get(
    "/{user_id}/todo-imports/{import_id}",
    response_model=TodoImportHttpView,
)
def get_todo_import(
    user_id: str,
    import_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoImportHttpView | JSONResponse:
    _require_path_user(user_id, resolved_user_id)
    view = read_todo_import(user_id=user_id, import_id=import_id)
    return _refused("not_found") if view is None else _view(user_id, view)


@router.put(
    "/{user_id}/todo-imports/{import_id}",
    response_model=TodoImportHttpView,
    description=(
        "Replace the whole draft, as the user edited it. 409 `stale_revision` "
        "when it changed since `expected_revision`; read it again."
    ),
)
def put_todo_import(
    user_id: str,
    import_id: str,
    body: TodoImportDraftRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoImportHttpView | JSONResponse:
    _require_path_user(user_id, resolved_user_id)
    written = write_todo_import_draft(
        user_id=user_id,
        import_id=import_id,
        draft=body.draft,
        expected_revision=body.expected_revision,
        clock=read_todo_clock(),
    )
    return _written(user_id, written)


@router.post(
    "/{user_id}/todo-imports/{import_id}/register",
    response_model=TodoImportHttpView,
    description=(
        "Create everything the draft at `expected_revision` names. Registering "
        "again returns the same import."
    ),
)
def register_todo_import_route(
    user_id: str,
    import_id: str,
    body: TodoImportRegisterRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoImportHttpView | JSONResponse:
    _require_path_user(user_id, resolved_user_id)
    registered = register_todo_import(
        user_id=user_id,
        import_id=import_id,
        expected_revision=body.expected_revision,
        clock=read_todo_clock(),
    )
    match registered:
        case StaleRevision():
            return _refused("stale_revision")
        case ImportClosed(state=closed):
            return _refused(closed)
        case DraftIssue(reason=reason):
            return _refused(reason, registered)
        case None:
            return _refused("not_found")
    view = read_todo_import(user_id=user_id, import_id=import_id)
    assert view is not None  # Registered just now; nothing deletes an import.
    return _view(user_id, view)


@router.post(
    "/{user_id}/todo-imports/{import_id}/cancel",
    response_model=TodoImportHttpView,
    description="Close the import without registering; canceling again returns it.",
)
def cancel_todo_import_route(
    user_id: str,
    import_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoImportHttpView | JSONResponse:
    _require_path_user(user_id, resolved_user_id)
    canceled = cancel_todo_import(
        user_id=user_id, import_id=import_id, clock=read_todo_clock()
    )
    return _written(user_id, canceled)

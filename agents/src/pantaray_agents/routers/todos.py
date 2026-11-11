"""The TODO list over HTTP. The user is the actor on every route; the AI acts
through its Action tools.

Each write is one store transaction; the view returned is read after it.
Failures carry an ``error_code``; none of them names a TODO's text.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.todos import core
from pantaray_agents.local_runtime.todos.clock import (
    TodoClock,
    read_todo_clock,
)
from pantaray_agents.local_runtime.todos.core import Rejected
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    ActiveRun,
    ApprovalPendingRun,
    Todo,
    TodoId,
)
from pantaray_agents.local_runtime.todos.reads import (
    TodoEntry,
    read_deleted_todos,
    read_todo,
    read_todo_list,
    read_todo_week,
)
from pantaray_agents.local_runtime.todos.week import effective_week
from pantaray_agents.local_runtime.todos.writes import (
    create_todo_item,
    set_todo_check,
    update_todo,
)
from pantaray_agents.schema.agent.base import ErrorType
from pantaray_agents.schema.todos import (
    DeletedTodoView,
    TodoCheckRequest,
    TodoCheckResult,
    TodoCreateRequest,
    TodoDeletionAnswerRequest,
    TodoDueRequest,
    TodoListPage,
    TodoListQuery,
    TodoScopeQuery,
    TodoStartRequest,
    TodoView,
    TodoWeekPage,
    TodoWeekQuery,
    deleted_todo_view,
    todo_view,
    todo_week_page,
)

router = APIRouter(prefix="/v1/agents/users", tags=["TODOs"])

# A refusal from the core or the store, keyed by its reason.
_REJECTIONS: Final[dict[str, tuple[int, str, ErrorType]]] = {
    "invalid_text": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "TODO_INVALID_TEXT",
        ErrorType.VALIDATION_ERROR,
    ),
    "project_not_found": (
        status.HTTP_404_NOT_FOUND,
        "PROJECT_NOT_FOUND",
        ErrorType.VALIDATION_ERROR,
    ),
    "client_todo_conflict": (
        status.HTTP_409_CONFLICT,
        "TODO_CLIENT_ID_CONFLICT",
        ErrorType.CONFLICT_ERROR,
    ),
    "deleted": (status.HTTP_409_CONFLICT, "TODO_DELETED", ErrorType.CONFLICT_ERROR),
    "stale_request": (
        status.HTTP_409_CONFLICT,
        "TODO_REQUEST_STALE",
        ErrorType.CONFLICT_ERROR,
    ),
    "expired": (
        status.HTTP_409_CONFLICT,
        "TODO_RESTORE_EXPIRED",
        ErrorType.CONFLICT_ERROR,
    ),
}


def _require_path_user(user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )


def _fail(reason: str) -> NoReturn:
    status_code, error_code, error_type = _REJECTIONS[reason]
    raise PublicAgentHTTPError(
        f"TODO request refused: {reason}",
        error_code=error_code,
        status_code=status_code,
        error_type=error_type,
    )


def _not_found() -> PublicAgentHTTPError:
    return PublicAgentHTTPError(
        "TODO not found",
        error_code="TODO_NOT_FOUND",
        status_code=status.HTTP_404_NOT_FOUND,
        error_type=ErrorType.VALIDATION_ERROR,
    )


def _busy() -> PublicAgentHTTPError:
    return PublicAgentHTTPError(
        "TODO conversation is running",
        error_code="TODO_BUSY",
        status_code=status.HTTP_409_CONFLICT,
        error_type=ErrorType.CONFLICT_ERROR,
    )


def _entry(user_id: str, todo_id: TodoId) -> TodoEntry:
    entry = read_todo(user_id=user_id, todo_id=todo_id)
    if entry is None:
        raise _not_found()
    return entry


def _view(entry: TodoEntry, clock: TodoClock) -> TodoView:
    placement = effective_week(entry.todo, clock.current_week_start, clock.zone)
    return todo_view(
        entry.todo,
        entry.conversation,
        carried=placement is not None and placement.carried,
        today=clock.today,
    )


def _read_view(user_id: str, todo_id: TodoId, clock: TodoClock) -> TodoView:
    return _view(_entry(user_id, todo_id), clock)


def _apply[R: str](
    user_id: str, todo_id: TodoId, step: Callable[[Todo], Todo | Rejected[R]]
) -> Todo:
    result = update_todo(user_id=user_id, todo_id=todo_id, step=step)
    if result is None:
        raise _not_found()
    if isinstance(result, Rejected):
        _fail(result.reason)
    return result


def _conversation_running(entry: TodoEntry) -> bool:
    conversation = entry.conversation
    return isinstance(conversation, ActionConversation) and isinstance(
        conversation.latest_run, ActiveRun | ApprovalPendingRun
    )


@router.get("/{user_id}/todos", response_model=TodoListPage)
def get_todo_list(
    user_id: str,
    query: Annotated[TodoListQuery, Query()],
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoListPage:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()
    todo_list = read_todo_list(
        user_id=user_id,
        scope=query.todo_scope(),
        project=query.project_filter(),
        sort=query.sort,
        clock=clock,
    )
    return TodoListPage(
        today=todo_list.today,
        items=tuple(_view(entry, clock) for entry in todo_list.items),
    )


@router.get("/{user_id}/todos/weeks", response_model=TodoWeekPage)
def get_todo_week(
    user_id: str,
    query: Annotated[TodoWeekQuery, Query()],
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoWeekPage:
    _require_path_user(user_id, resolved_user_id)
    week = read_todo_week(
        user_id=user_id,
        scope=query.todo_scope(),
        project=query.project_filter(),
        week_of=None if query.week_start is None else query.week_start.isoformat(),
        clock=read_todo_clock(),
    )
    return todo_week_page(week)


@router.post(
    "/{user_id}/todos",
    response_model=TodoView,
    description=(
        "Write the user's TODO. Resending the same `client_todo_id` with the "
        "same TODO returns it; with a different one, 409."
    ),
)
def post_todo(
    user_id: str,
    body: TodoCreateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoView:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()
    created = create_todo_item(
        user_id=user_id,
        client_todo_id=body.client_todo_id,
        project_id=body.project_id,
        text=body.text,
        starts_on=clock.today if body.starts_on is None else body.starts_on.isoformat(),
        due=None if body.due is None else body.due.due(),
        clock=clock,
    )
    if isinstance(created, Rejected):
        _fail(created.reason)
    return _read_view(user_id, created.todo_id, clock)


@router.put("/{user_id}/todos/{todo_id}/check", response_model=TodoCheckResult)
def put_todo_check(
    user_id: str,
    todo_id: str,
    body: TodoCheckRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoCheckResult:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()
    result = set_todo_check(
        user_id=user_id, todo_id=todo_id, by="user", checked=body.checked, clock=clock
    )
    if result is None:
        raise _not_found()
    if isinstance(result, Rejected):
        # The user may remove any check, so `ai_cannot_uncheck` never comes back.
        _fail(result.reason)
    next_entry = (
        None
        if result.next_todo is None
        else read_todo(user_id=user_id, todo_id=result.next_todo.todo_id)
    )
    return TodoCheckResult(
        todo=_read_view(user_id, todo_id, clock),
        next_todo=None if next_entry is None else _view(next_entry, clock),
    )


@router.put("/{user_id}/todos/{todo_id}/due", response_model=TodoView)
def put_todo_due(
    user_id: str,
    todo_id: str,
    body: TodoDueRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoView:
    _require_path_user(user_id, resolved_user_id)
    due = None if body.due is None else body.due.due()
    _apply(user_id, todo_id, lambda todo: core.set_due(todo, due))
    return _read_view(user_id, todo_id, read_todo_clock())


@router.put("/{user_id}/todos/{todo_id}/start", response_model=TodoView)
def put_todo_start(
    user_id: str,
    todo_id: str,
    body: TodoStartRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoView:
    _require_path_user(user_id, resolved_user_id)
    starts_on = body.starts_on.isoformat()
    _apply(user_id, todo_id, lambda todo: core.set_start(todo, starts_on))
    return _read_view(user_id, todo_id, read_todo_clock())


@router.delete(
    "/{user_id}/todos/{todo_id}",
    response_model=TodoView,
    description=(
        "Delete the TODO; 200 also when it is already deleted. 409 `TODO_BUSY` "
        "while its conversation runs or waits for approval."
    ),
)
def delete_todo(
    user_id: str,
    todo_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoView:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()
    entry = _entry(user_id, todo_id)
    if entry.todo.deletion is not None:
        return _view(entry, clock)
    if _conversation_running(entry):
        raise _busy()
    result = update_todo(
        user_id=user_id,
        todo_id=todo_id,
        step=lambda todo: core.delete_by_user(todo, now=clock.now),
    )
    if result is None:
        raise _not_found()
    # `already_deleted` here is a concurrent delete: the TODO is deleted either way.
    return _read_view(user_id, todo_id, clock)


@router.post(
    "/{user_id}/todos/{todo_id}/deletion-requests/{request_id}/answer",
    response_model=TodoView,
    description=(
        "Answer the AI's pending deletion request. 409 `TODO_REQUEST_STALE` "
        "once it is no longer pending; approving is 409 `TODO_BUSY` while the "
        "conversation runs or waits for approval."
    ),
)
def answer_todo_deletion_request(
    user_id: str,
    todo_id: str,
    request_id: str,
    body: TodoDeletionAnswerRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoView:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()

    def answer(todo: Todo) -> Todo | Rejected[core.DeletionAnswerRejection]:
        return core.answer_deletion(
            todo, request_id=request_id, answer=body.answer, now=clock.now
        )

    entry = _entry(user_id, todo_id)
    # Only an approval that would delete waits for the run; a stale one says so.
    if (
        body.answer == "approve"
        and _conversation_running(entry)
        and isinstance(answer(entry.todo), Todo)
    ):
        raise _busy()
    _apply(user_id, todo_id, answer)
    return _read_view(user_id, todo_id, clock)


@router.post(
    "/{user_id}/todos/{todo_id}/restore",
    response_model=TodoView,
    description=(
        "Restore a deleted TODO; 200 also when it is not deleted. 409 "
        "`TODO_RESTORE_EXPIRED` once the retention period has passed."
    ),
)
def restore_todo(
    user_id: str,
    todo_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoView:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()
    result = update_todo(
        user_id=user_id,
        todo_id=todo_id,
        step=lambda todo: core.restore(todo, now=clock.now),
    )
    if result is None:
        raise _not_found()
    if isinstance(result, Rejected) and result.reason == "expired":
        _fail(result.reason)
    return _read_view(user_id, todo_id, clock)


@router.get("/{user_id}/todos/deleted", response_model=tuple[DeletedTodoView, ...])
def get_deleted_todos(
    user_id: str,
    query: Annotated[TodoScopeQuery, Query()],
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> tuple[DeletedTodoView, ...]:
    _require_path_user(user_id, resolved_user_id)
    clock = read_todo_clock()
    todos = read_deleted_todos(user_id=user_id, scope=query.todo_scope(), clock=clock)
    return tuple(deleted_todo_view(todo, clock.zone) for todo in todos)

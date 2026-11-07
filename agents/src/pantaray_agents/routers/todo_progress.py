"""Goals, progress records and the progress view over HTTP.

The user is the actor on every route: only they set goals, and a record they
add needs no other evidence. Failures carry an ``error_code`` and name no text.
"""

from __future__ import annotations

from typing import Annotated, Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.todos.clock import (
    read_todo_clock,
)
from pantaray_agents.local_runtime.todos.core import Rejected
from pantaray_agents.local_runtime.todos.goal_store import (
    add_user_progress_record,
    clear_project_goal,
    delete_progress_record,
    read_progress,
    set_project_goal,
)
from pantaray_agents.schema.agent.base import ErrorType
from pantaray_agents.schema.todo_progress import (
    GoalView,
    ProgressRecordCreateRequest,
    ProgressRecordView,
    TodoGoalRequest,
    TodoProgressPage,
    TodoProgressQuery,
)

router = APIRouter(prefix="/v1/agents/users", tags=["TODO progress"])

_REJECTIONS: Final[dict[str, tuple[int, str, ErrorType]]] = {
    "project_not_found": (
        status.HTTP_404_NOT_FOUND,
        "PROJECT_NOT_FOUND",
        ErrorType.VALIDATION_ERROR,
    ),
    "invalid_text": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "TODO_PROGRESS_INVALID_TEXT",
        ErrorType.VALIDATION_ERROR,
    ),
    "deadline_before_set": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "TODO_GOAL_DEADLINE_BEFORE_SET",
        ErrorType.VALIDATION_ERROR,
    ),
    "deleted": (status.HTTP_409_CONFLICT, "TODO_DELETED", ErrorType.CONFLICT_ERROR),
    "record_conflict": (
        status.HTTP_409_CONFLICT,
        "TODO_PROGRESS_CLIENT_ID_CONFLICT",
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
        f"TODO progress request refused: {reason}",
        error_code=error_code,
        status_code=status_code,
        error_type=error_type,
    )


@router.put(
    "/{user_id}/todos/projects/{project_id}/goal",
    response_model=GoalView,
    description=(
        "Set or edit the project's goal. An edit keeps when the goal was first "
        "set, so records keep counting toward it; the same goal again changes "
        "nothing."
    ),
)
def put_project_goal(
    user_id: str,
    project_id: str,
    body: TodoGoalRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> GoalView:
    _require_path_user(user_id, resolved_user_id)
    goal = set_project_goal(
        user_id=user_id,
        project_id=project_id,
        text=body.text,
        target=body.target,
        unit=body.unit,
        deadline=body.deadline.isoformat(),
        clock=read_todo_clock(),
    )
    if isinstance(goal, Rejected):
        _fail(goal.reason)
    return GoalView.model_validate(goal)


@router.delete(
    "/{user_id}/todos/projects/{project_id}/goal",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Clear the project's goal; 204 also when it has none.",
)
def delete_project_goal(
    user_id: str,
    project_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    _require_path_user(user_id, resolved_user_id)
    cleared = clear_project_goal(user_id=user_id, project_id=project_id)
    if isinstance(cleared, Rejected):
        _fail(cleared.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{user_id}/todos/{todo_id}/progress-records",
    response_model=ProgressRecordView,
    description=(
        "Add the user's progress record to the TODO. Resending the same "
        "`client_record_id` with the same record returns it; with a different "
        "one, 409."
    ),
)
def post_progress_record(
    user_id: str,
    todo_id: str,
    body: ProgressRecordCreateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ProgressRecordView:
    _require_path_user(user_id, resolved_user_id)
    record = add_user_progress_record(
        user_id=user_id,
        todo_id=todo_id,
        client_record_id=body.client_record_id,
        quantity=body.quantity,
        unit=body.unit,
        note=body.note,
        clock=read_todo_clock(),
    )
    if record is None:
        raise PublicAgentHTTPError(
            "TODO not found",
            error_code="TODO_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
            error_type=ErrorType.VALIDATION_ERROR,
        )
    if isinstance(record, Rejected):
        _fail(record.reason)
    return ProgressRecordView.model_validate(record)


@router.delete(
    "/{user_id}/todos/progress-records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Delete a progress record, the AI's too; 204 also when it is gone.",
)
def delete_progress_record_route(
    user_id: str,
    record_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    _require_path_user(user_id, resolved_user_id)
    delete_progress_record(user_id=user_id, record_id=record_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{user_id}/todos/progress", response_model=TodoProgressPage)
def get_todo_progress(
    user_id: str,
    query: Annotated[TodoProgressQuery, Query()],
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> TodoProgressPage:
    _require_path_user(user_id, resolved_user_id)
    view = read_progress(
        user_id=user_id,
        scope=query.todo_scope(),
        project=query.project_filter(),
        period=query.period,
        clock=read_todo_clock(),
    )
    return TodoProgressPage.model_validate(view)

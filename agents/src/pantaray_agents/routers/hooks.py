"""The tasks Actions scheduled, and the owner's switch for them, over HTTP.

The owner is the actor on every route. Built-in hooks are not listed or
switched here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.hooks.gates import build_gates
from pantaray_agents.local_runtime.hooks.listing import list_hooks, set_hook_enabled
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.schema.agent.base import ErrorType
from pantaray_agents.schema.hooks import (
    HookEnabledRequest,
    HookListResponse,
    TaskHookResponse,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Hooks"])


def _require_path_user(user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )


@router.get(
    "/{user_id}/hooks",
    response_model=HookListResponse,
    description=(
        "Every task an Action scheduled, with its state, and whether recording "
        "runs for the tasks that fire on activity."
    ),
)
def get_hooks(
    user_id: str, resolved_user_id: str = Depends(get_current_user_id_from_token)
) -> HookListResponse:
    _require_path_user(user_id, resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return HookListResponse(
        hooks=tuple(
            TaskHookResponse.model_validate(view)
            for view in list_hooks(
                db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
            )
        ),
        recording_ready=build_gates(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        )["recording_ready"](),
    )


@router.put(
    "/{user_id}/hooks/{hook_id}/enabled",
    status_code=status.HTTP_204_NO_CONTENT,
    description=(
        "Switch a task on or off. Switching a stopped task on clears its failures."
    ),
)
def put_hook_enabled(
    user_id: str,
    hook_id: str,
    body: HookEnabledRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    _require_path_user(user_id, resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    if not set_hook_enabled(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        hook_id=hook_id,
        enabled=body.enabled,
    ):
        raise PublicAgentHTTPError(
            "hook change refused: not_found",
            error_code="HOOK_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
            error_type=ErrorType.VALIDATION_ERROR,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)

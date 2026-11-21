"""Every hook's state, and the owner's switches and task approvals, over HTTP.

The owner is the actor on every route: an agent never approves a task or
switches a hook here.
"""

from __future__ import annotations

from typing import Final, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Response, status

from pantaray_agents.agents.core import PublicAgentHTTPError
from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.hooks.listing import (
    BuiltinHookView,
    HookChangeRefusal,
    approve_task,
    list_hooks,
    set_hook_enabled,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.schema.agent.base import ErrorType
from pantaray_agents.schema.hooks import (
    BuiltinHookResponse,
    HookApprovalRequest,
    HookEnabledRequest,
    HookListResponse,
    TaskHookResponse,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Hooks"])

_REFUSALS: Final[dict[HookChangeRefusal, tuple[int, str, ErrorType]]] = {
    "not_found": (
        status.HTTP_404_NOT_FOUND,
        "HOOK_NOT_FOUND",
        ErrorType.VALIDATION_ERROR,
    ),
    "not_a_task": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "HOOK_NOT_A_TASK",
        ErrorType.VALIDATION_ERROR,
    ),
    "invalid": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "HOOK_TASK_INVALID",
        ErrorType.VALIDATION_ERROR,
    ),
    "changed": (
        status.HTTP_409_CONFLICT,
        "HOOK_TASK_CHANGED",
        ErrorType.CONFLICT_ERROR,
    ),
}


def _require_path_user(user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )


def _fail(refusal: HookChangeRefusal) -> NoReturn:
    status_code, error_code, error_type = _REFUSALS[refusal]
    raise PublicAgentHTTPError(
        f"hook change refused: {refusal}",
        error_code=error_code,
        status_code=status_code,
        error_type=error_type,
    )


@router.get(
    "/{user_id}/hooks",
    response_model=HookListResponse,
    description="Every built-in hook, then every task file, with its state.",
)
def get_hooks(
    user_id: str, resolved_user_id: str = Depends(get_current_user_id_from_token)
) -> HookListResponse:
    _require_path_user(user_id, resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return HookListResponse(
        hooks=tuple(
            BuiltinHookResponse.model_validate(view)
            if isinstance(view, BuiltinHookView)
            else TaskHookResponse.model_validate(view)
            for view in list_hooks(
                db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
            )
        )
    )


@router.put(
    "/{user_id}/hooks/{hook_id}/approval",
    status_code=status.HTTP_204_NO_CONTENT,
    description=(
        "Approve a task's content, named by the hash the list gave; 409 when "
        "the file changed since. A later change stops it until approved again."
    ),
)
def put_hook_approval(
    user_id: str,
    hook_id: str,
    body: HookApprovalRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    _require_path_user(user_id, resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    refusal = approve_task(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        hook_id=hook_id,
        content_hash=body.content_hash,
    )
    if refusal is not None:
        _fail(refusal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{user_id}/hooks/{hook_id}/enabled",
    status_code=status.HTTP_204_NO_CONTENT,
    description=(
        "Switch a built-in hook or a task on or off. Switching a stopped task "
        "on clears its failures."
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
    refusal = set_hook_enabled(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        hook_id=hook_id,
        enabled=body.enabled,
    )
    if refusal is not None:
        _fail(refusal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

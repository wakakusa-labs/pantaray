from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.identity import (
    verify_current_owner,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
)
from pantaray_agents.local_runtime.tooling.repository import (
    ActionApprovalModeOwnerError,
    load_action_approval_mode,
    load_effective_approval_preference,
    set_action_approval_mode,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Action Agent"])

ACTION_NOT_FOUND_ERROR_CODE = "ACTION_NOT_FOUND"


class ActionApprovalModeResponse(BaseModel):
    action_id: str
    approval_mode: Literal["prompt_each_time", "always_allow"]
    source: Literal["action", "user_default"] = Field(
        description="Where the effective mode comes from"
    )


class ActionApprovalModeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    approval_mode: Literal["prompt_each_time", "always_allow"]


@router.get(
    "/{user_id}/actions/{action_id}/approval-mode",
    response_model=ActionApprovalModeResponse,
)
async def get_action_approval_mode(
    user_id: str,
    action_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionApprovalModeResponse:
    _authorize(resolved_user_id=resolved_user_id, user_id=user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    override = load_action_approval_mode(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        action_id=action_id,
    )
    if override is not None:
        return ActionApprovalModeResponse(
            action_id=action_id,
            approval_mode=override,
            source="action",
        )
    preference = load_effective_approval_preference(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        applies_to=APPROVAL_SCOPE_WORKSPACE_EDIT_AND_COMMAND,
    )
    return ActionApprovalModeResponse(
        action_id=action_id,
        approval_mode=preference.approval_mode,
        source="user_default",
    )


@router.put(
    "/{user_id}/actions/{action_id}/approval-mode",
    response_model=ActionApprovalModeResponse,
)
async def update_action_approval_mode(
    user_id: str,
    action_id: str,
    body: ActionApprovalModeUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionApprovalModeResponse:
    _authorize(resolved_user_id=resolved_user_id, user_id=user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        set_action_approval_mode(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            approval_mode=body.approval_mode,
            updated_at=now_utc_iso(),
        )
    except ActionApprovalModeOwnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ACTION_NOT_FOUND_ERROR_CODE,
                "message": str(exc),
            },
        ) from exc
    return ActionApprovalModeResponse(
        action_id=action_id,
        approval_mode=body.approval_mode,
        source="action",
    )


def _authorize(*, resolved_user_id: str, user_id: str) -> None:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id mismatch",
        )
    verify_current_owner(user_id)


__all__ = [
    "ActionApprovalModeResponse",
    "ActionApprovalModeUpdateRequest",
    "get_action_approval_mode",
    "router",
    "update_action_approval_mode",
]

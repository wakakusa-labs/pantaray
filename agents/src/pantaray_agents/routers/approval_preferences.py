from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.brokering.approval_identity import (
    build_approval_preference_id,
    build_capability_grant_id,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    apply_approval_preference_setting,
    load_global_approval_preference,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Approval Preferences"])

_APPLIES_TO_WORKSPACE_EDIT_AND_COMMAND = "workspace_edit_and_command"
_APPROVAL_MODE_PROMPT_EACH_TIME = "prompt_each_time"
_APPROVAL_MODE_ALWAYS_ALLOW = "always_allow"
_GLOBAL_SCOPE_TYPE = "global"
_GLOBAL_REQUIRED_CAPABILITIES = ("scoped_write", "process_exec_local")


class ApprovalPreferenceResponse(BaseModel):
    scope_type: Literal["global"] = Field(description="Preference scope type")
    scope_ref: None = Field(default=None, description="Global scope has no scope_ref")
    approval_mode: Literal["prompt_each_time", "always_allow"]
    applies_to: tuple[Literal["workspace_edit_and_command"], ...]


class ApprovalPreferenceUpdateRequest(BaseModel):
    approval_mode: Literal["prompt_each_time", "always_allow"]


@router.get(
    "/{user_id}/approval-preferences/workspace-edit-and-command",
    response_model=ApprovalPreferenceResponse,
)
async def get_workspace_edit_and_command_approval_preference(
    user_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ApprovalPreferenceResponse:
    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        preference = load_global_approval_preference(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            applies_to=_APPLIES_TO_WORKSPACE_EDIT_AND_COMMAND,
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load approval preference: {exc}",
        ) from exc

    return ApprovalPreferenceResponse(
        scope_type="global",
        scope_ref=None,
        approval_mode=preference.approval_mode,
        applies_to=(_APPLIES_TO_WORKSPACE_EDIT_AND_COMMAND,),
    )


@router.put(
    "/{user_id}/approval-preferences/workspace-edit-and-command",
    response_model=ApprovalPreferenceResponse,
)
async def update_workspace_edit_and_command_approval_preference(
    user_id: str,
    body: ApprovalPreferenceUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ApprovalPreferenceResponse:
    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    updated_at = now_utc_iso()
    preference = ApprovalPreferenceUpsertInput(
        preference_id=build_approval_preference_id(
            user_id=user_id,
            scope_type=_GLOBAL_SCOPE_TYPE,
            scope_ref=None,
            applies_to=(_APPLIES_TO_WORKSPACE_EDIT_AND_COMMAND,),
        ),
        user_id=user_id,
        scope_type=_GLOBAL_SCOPE_TYPE,
        scope_ref=None,
        approval_mode=body.approval_mode,
        applies_to=(_APPLIES_TO_WORKSPACE_EDIT_AND_COMMAND,),
        created_at=updated_at,
        updated_at=updated_at,
    )
    grants = (
        tuple(
            CapabilityGrantCreateInput(
                grant_id=build_capability_grant_id(
                    user_id=user_id,
                    scope_type=_GLOBAL_SCOPE_TYPE,
                    scope_ref=None,
                    capability=capability,
                    applies_to=preference.applies_to,
                ),
                user_id=user_id,
                preference_id=preference.preference_id,
                capability=capability,
                scope_type=_GLOBAL_SCOPE_TYPE,
                scope_ref=None,
                grant_source="settings",
                granted_at=updated_at,
            )
            for capability in _GLOBAL_REQUIRED_CAPABILITIES
        )
        if body.approval_mode == _APPROVAL_MODE_ALWAYS_ALLOW
        else ()
    )

    try:
        apply_approval_preference_setting(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            preference=preference,
            grants=grants,
            revoked_at=updated_at,
            revocation_reason="approval preference changed from settings",
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update approval preference: {exc}",
        ) from exc

    return ApprovalPreferenceResponse(
        scope_type="global",
        scope_ref=None,
        approval_mode=body.approval_mode,
        applies_to=(_APPLIES_TO_WORKSPACE_EDIT_AND_COMMAND,),
    )

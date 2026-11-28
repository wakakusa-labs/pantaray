from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.action_approval_decision import (
    ActionApprovalDecision,
    ActionApprovalDecisionCommand,
    ActionApprovalDecisionError,
    apply_action_approval_decision,
)
from pantaray_agents.local_runtime.runtime.action_subagent_approval import (
    ActionSubagentApprovalDecisionError,
    apply_action_subagent_approval_decision,
)
from pantaray_agents.local_runtime.runtime.identity import (
    verify_current_owner,
)
from pantaray_agents.local_runtime.runtime.job_types import (
    ACTION_PROCESS_KIND,
    ACTION_SUBAGENT_PROCESS_KIND,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Action Agent"])

APPROVAL_NOT_FOUND_ERROR_CODE = "APPROVAL_NOT_FOUND"
APPROVAL_DECISION_CONFLICT_ERROR_CODE = "APPROVAL_DECISION_CONFLICT"
APPROVAL_INTERRUPTED_ERROR_CODE = "APPROVAL_INTERRUPTED"
APPROVAL_REQUEST_MISMATCH_ERROR_CODE = "APPROVAL_REQUEST_MISMATCH"


class ActionApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: ActionApprovalDecision = Field(description="承認パネルで選択した決定")
    process_id: str = Field(description="承認対象の Action process ID")
    tool_request_id: str = Field(description="承認対象の logical tool request ID")
    approval_session_id: str = Field(description="承認対象の approval session ID")

    @field_validator("process_id", "tool_request_id", "approval_session_id")
    @classmethod
    def _require_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("approval identity must be a non-empty string")
        return normalized


class ActionApprovalDecisionResponse(BaseModel):
    process_id: str
    approval_session_id: str
    decision: ActionApprovalDecision
    accepted: Literal[True]


@router.post(
    "/{user_id}/actions/{action_id}/approvals",
    response_model=ActionApprovalDecisionResponse,
    status_code=status.HTTP_200_OK,
)
async def decide_action_tool_approval(
    user_id: str,
    action_id: str,
    body: ActionApprovalDecisionRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ActionApprovalDecisionResponse:
    if resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id mismatch",
        )
    verify_current_owner(user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        kind = _read_approval_process_kind(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            process_id=body.process_id,
        )
        if kind == ACTION_PROCESS_KIND:
            apply_action_approval_decision(
                ActionApprovalDecisionCommand(
                    user_id=user_id,
                    action_id=action_id,
                    process_id=body.process_id,
                    tool_request_id=body.tool_request_id,
                    approval_session_id=body.approval_session_id,
                    decision=body.decision,
                )
            )
        elif body.decision == "approved_for_conversation":
            # A subagent is never asked about a folder outside the workspace.
            raise ActionApprovalDecisionError(
                "Only an outside-workspace approval can be allowed for the conversation",
                error_code=APPROVAL_REQUEST_MISMATCH_ERROR_CODE,
            )
        else:
            apply_action_subagent_approval_decision(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                action_id=action_id,
                child_process_id=body.process_id,
                approval_session_id=body.approval_session_id,
                tool_request_id=body.tool_request_id,
                decision=body.decision,
            )
    except (ActionApprovalDecisionError, ActionSubagentApprovalDecisionError) as exc:
        raise _approval_http_error(exc) from exc
    # Each decision settles exactly the physical process the approval panel
    # selected, so the response identifies that process and is never normalized
    # to the root the client streams from.
    return ActionApprovalDecisionResponse(
        process_id=body.process_id,
        approval_session_id=body.approval_session_id,
        decision=body.decision,
        accepted=True,
    )


def _read_approval_process_kind(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    action_id: str,
    process_id: str,
) -> str:
    """Return the immutable physical kind that owns the requested decision."""

    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            "SELECT kind FROM processes "
            "WHERE process_id=? AND user_id=? AND action_id=? AND kind IN (?,?)",
            (
                process_id,
                user_id,
                action_id,
                ACTION_PROCESS_KIND,
                ACTION_SUBAGENT_PROCESS_KIND,
            ),
        ).fetchone()
    if row is None:
        raise ActionApprovalDecisionError(
            "Approval process not found",
            error_code=APPROVAL_NOT_FOUND_ERROR_CODE,
        )
    return str(row[0])


def _approval_http_error(
    exc: ActionApprovalDecisionError | ActionSubagentApprovalDecisionError,
) -> HTTPException:
    if exc.error_code == APPROVAL_NOT_FOUND_ERROR_CODE:
        status_code = status.HTTP_404_NOT_FOUND
    elif exc.error_code == APPROVAL_REQUEST_MISMATCH_ERROR_CODE:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(
        status_code=status_code,
        detail={
            "error_code": exc.error_code,
            "message": str(exc),
        },
    )


__all__ = [
    "ActionApprovalDecisionRequest",
    "ActionApprovalDecisionResponse",
    "decide_action_tool_approval",
    "router",
]

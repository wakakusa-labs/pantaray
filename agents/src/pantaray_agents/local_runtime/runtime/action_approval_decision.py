"""The user's decision on one Action tool approval card."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.tooling.outside_workspace_grant import (
    OutsideWorkspaceGrantError,
    grant_outside_workspace_folder_in_connection,
)

from .action_approval import (
    ActionApprovalDecisionError,
    ActionApprovalTarget,
    resume_paused_action_approval_in_connection,
)
from .identity import verify_current_owner
from .runtime_env import read_local_runtime_db_config
from .utc_timestamps import now_utc_iso

# "approved_for_conversation" approves the call once, like "approved_once", and
# also opens its outside-workspace folder for the rest of this Action.
ActionApprovalDecision = Literal["approved_once", "approved_for_conversation", "denied"]


def _require_non_blank(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must be a non-empty string")
    return normalized


class ActionApprovalDecisionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    process_id: str = Field(min_length=1)
    tool_request_id: str = Field(min_length=1)
    approval_session_id: str = Field(min_length=1)
    decision: ActionApprovalDecision

    _validate_user_id = field_validator("user_id")(_require_non_blank)
    _validate_action_id = field_validator("action_id")(_require_non_blank)
    _validate_process_id = field_validator("process_id")(_require_non_blank)
    _validate_tool_request_id = field_validator("tool_request_id")(_require_non_blank)
    _validate_approval_session_id = field_validator("approval_session_id")(
        _require_non_blank
    )


@dataclass(frozen=True, slots=True)
class ActionApprovalDecisionResult:
    approval_session_id: str
    process_id: str
    job_id: str
    decision: ActionApprovalDecision


def apply_action_approval_decision(
    command: ActionApprovalDecisionCommand,
) -> ActionApprovalDecisionResult:
    """Apply one decision and resume its paused Action runtime atomically."""

    verify_current_owner(command.user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    decided_at = now_utc_iso()
    target = ActionApprovalTarget(
        user_id=command.user_id,
        action_id=command.action_id,
        process_id=command.process_id,
        tool_request_id=command.tool_request_id,
        approval_session_id=command.approval_session_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            job_id = resume_paused_action_approval_in_connection(
                connection,
                target=target,
                session_status=_session_status(command.decision),
                decided_at=decided_at,
            )
            if command.decision == "approved_for_conversation":
                try:
                    grant_outside_workspace_folder_in_connection(
                        connection,
                        user_id=command.user_id,
                        action_id=command.action_id,
                        approval_session_id=command.approval_session_id,
                        db_path=db_path,
                        granted_at=decided_at,
                    )
                except OutsideWorkspaceGrantError as exc:
                    raise ActionApprovalDecisionError(
                        str(exc), error_code="APPROVAL_REQUEST_MISMATCH"
                    ) from exc

    return ActionApprovalDecisionResult(
        approval_session_id=command.approval_session_id,
        process_id=command.process_id,
        job_id=job_id,
        decision=command.decision,
    )


def _session_status(
    decision: ActionApprovalDecision,
) -> Literal["approved_once", "denied"]:
    return "denied" if decision == "denied" else "approved_once"


__all__ = [
    "ActionApprovalDecision",
    "ActionApprovalDecisionCommand",
    "ActionApprovalDecisionError",
    "ActionApprovalDecisionResult",
    "apply_action_approval_decision",
]

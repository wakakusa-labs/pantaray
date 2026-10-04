"""Turn an approval outcome into the step the Action pauses or continues on.

Every consent-gated tool ends an approval the same way, whatever it does once
approved: a pending decision becomes a processing step the Overlay can answer, and
a denial becomes a successful step that reports the tool was skipped. Keeping that
shape in one place is what makes a new gated tool behave like the existing ones.
"""

from __future__ import annotations

from pathlib import Path

from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerPolicyError,
)
from pantaray_agents.local_runtime.tooling.repository.approval_sessions import (
    load_approval_session_by_request,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    ApprovalCommandSummaryToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import (
    APPROVAL_DENIED_OUTPUT_KIND,
    APPROVAL_REQUIRED_OUTPUT_KIND,
    ApprovalDeniedToolControl,
    ApprovalRequiredToolControl,
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
)


def build_approval_required_preparation(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    step_id: str,
    tool_def: ToolDefinition,
    tool_request_id: str,
    approval_session_id: str,
    user_id: str,
    manifest_id: str,
    action_id: str,
) -> ToolExecutionPreparation:
    """The processing step that carries a pending decision to the Overlay.

    Raises:
        BrokerPolicyError: the pending session the caller was told about is gone.
    """

    approval_session = load_approval_session_by_request(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        tool_request_id=tool_request_id,
    )
    if approval_session is None:
        raise BrokerPolicyError(
            "approval-required tool result is missing approval session"
        )
    projected_summary = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=ToolResultFinalizationRequest(
            owner=ApprovalCommandSummaryToolResultOwner(
                manifest_id=manifest_id,
                action_id=action_id,
                user_id=user_id,
                tool_request_id=tool_request_id,
            ),
            output=dict(approval_session.command_summary_json),
        ),
    )
    if not isinstance(projected_summary.output, dict):
        raise RuntimeError("approval command summary projection must be an object")
    completed_at = now_utc_iso()
    output: dict[str, JSONValue] = {
        "kind": APPROVAL_REQUIRED_OUTPUT_KIND,
        "approval_status": "pending",
        "approval_session_id": approval_session_id,
        "tool_request_id": tool_request_id,
        "intent_class": approval_session.intent_class,
        "command_summary": projected_summary.output,
        "command_summary_storage_kind": projected_summary.storage_kind,
    }
    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="processing",
            started_at=completed_at,
            completed_at=completed_at,
            output=output,
        ),
        control=ApprovalRequiredToolControl(
            approval_session_id=approval_session_id,
            tool_request_id=tool_request_id,
            intent_class=approval_session.intent_class,
            command_summary=projected_summary.output,
        ),
    )


def build_approval_denied_preparation(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    tool_request_id: str,
    approval_session_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    """A denial is a successful step: the call was skipped, not attempted."""

    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="success",
            started_at=requested_at,
            completed_at=now_utc_iso(),
            output={
                "kind": APPROVAL_DENIED_OUTPUT_KIND,
                "approval_status": "denied",
                "approval_session_id": approval_session_id,
                "tool_request_id": tool_request_id,
                "tool_id": tool_def.tool_id,
                "executed": False,
                "message": (
                    "The requested tool call was denied by the user. "
                    "Treat this tool call as skipped and continue."
                ),
            },
        ),
        control=ApprovalDeniedToolControl(
            approval_session_id=approval_session_id,
            tool_request_id=tool_request_id,
        ),
    )


__all__ = [
    "build_approval_denied_preparation",
    "build_approval_required_preparation",
]

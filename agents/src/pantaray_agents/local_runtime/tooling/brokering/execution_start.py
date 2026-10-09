"""Consume an approval at execution start, so one consent runs one tool call.

Every consent-gated tool - brokered or not - reaches execution through
:func:`claim_approved_execution_start`. The claim is what makes an approved
session single-use: it binds the session to the invocation that is about to run
and stamps ``claimed_at`` in the same transaction, so a replay after a resume or
a crash finds the session already spent and is refused instead of running a
second time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from ..action_subagent_resource_authority import (
    ActionSubagentResourceActorError,
    ActionSubagentResourceWriteDeniedError,
)
from ..models import ToolInvocationStartInput
from ..repository import claim_approval_execution_start
from .action_subagent_broker_authority import ACTION_SUBAGENT_WRITE_DENIED
from .broker_common import BrokerContext
from .broker_protocol import (
    ValidatedCommandRequest,
    ValidatedGlobRequest,
    ValidatedGrepRequest,
    ValidatedListRequest,
    ValidatedPatchRequest,
    ValidatedReadRequest,
    ValidatedRenderPdfPageRequest,
)
from .direct_execution_start import (
    build_direct_invocation_start,
    should_start_direct_invocation,
    start_direct_execution,
)


def claim_approved_execution_start(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    tool_request_id: str,
    action_id: str,
    approval_session_id: str | None,
    approval_source: Literal["settings", "prompt"],
    tool_invocation_id: str,
    started_at: str,
    invocation: ToolInvocationStartInput | None,
) -> None:
    """Spend the approval session that authorizes this invocation.

    ``invocation`` is the audit row the claim records atomically with the claim
    itself, and that row must not exist yet; pass ``None`` only when the caller
    already recorded its own row for this same request.

    Raises:
        BrokerPolicyError: the session was already claimed, or it no longer
            matches the invocation asking to run.
    """

    claim_result = claim_approval_execution_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        tool_request_id=tool_request_id,
        action_id=action_id,
        approval_session_id=approval_session_id,
        approval_source=approval_source,
        tool_invocation_id=tool_invocation_id,
        started_at=started_at,
        invocation=invocation,
    )
    if claim_result.status != "updated":
        raise BrokerPolicyError(
            "approval session could not be claimed for tool execution"
        )


def claim_approval_gated_execution_start(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    context: BrokerContext,
    validated: ValidatedPatchRequest | ValidatedCommandRequest,
    args: dict[str, JSONValue],
) -> str:
    if validated.approval_session_id is None or validated.approval_source is None:
        raise BrokerPolicyError("approval-gated execution requires an approval session")
    tool_invocation_id = validated.tool_invocation_id
    if tool_invocation_id is None:
        raise BrokerPolicyError("approval-gated execution requires an invocation id")
    if not should_start_direct_invocation(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        tool_invocation_id=tool_invocation_id,
    ):
        # A brokered invocation row is written by the claim below and by nothing
        # else, so finding one already there means this execution has started
        # before - refuse rather than run it a second time.
        raise BrokerPolicyError("tool invocation has already been claimed")
    claim_approved_execution_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=context.execution_session.user_id,
        tool_request_id=validated.tool_request_id,
        action_id=validated.action_id,
        approval_session_id=validated.approval_session_id,
        approval_source=validated.approval_source,
        tool_invocation_id=tool_invocation_id,
        started_at=validated.requested_at,
        invocation=build_direct_invocation_start(
            context=context,
            validated=validated,
            args=args,
        ),
    )
    return tool_invocation_id


def claim_broker_execution_start(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    context: BrokerContext,
    validated: (
        ValidatedReadRequest
        | ValidatedRenderPdfPageRequest
        | ValidatedListRequest
        | ValidatedGlobRequest
        | ValidatedGrepRequest
        | ValidatedPatchRequest
        | ValidatedCommandRequest
    ),
    args: dict[str, JSONValue],
) -> str | None:
    try:
        if isinstance(validated, (ValidatedPatchRequest, ValidatedCommandRequest)):
            if (
                validated.approval_session_id is not None
                and validated.approval_source is not None
            ):
                return claim_approval_gated_execution_start(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    context=context,
                    validated=validated,
                    args=args,
                )
        return start_direct_execution(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            context=context,
            validated=validated,
            args=args,
        )
    except (
        ActionSubagentResourceActorError,
        ActionSubagentResourceWriteDeniedError,
    ) as exc:
        raise BrokerPolicyError(str(exc), code=ACTION_SUBAGENT_WRITE_DENIED) from exc


__all__ = [
    "claim_approval_execution_start",
    "claim_approval_gated_execution_start",
    "claim_approved_execution_start",
    "claim_broker_execution_start",
]

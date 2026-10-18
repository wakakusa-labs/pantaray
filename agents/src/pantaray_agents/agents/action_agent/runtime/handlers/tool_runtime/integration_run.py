"""integration_run_tool as an Action tool: resolve, settle consent, execute, wrap.

Resolution and execution are the shared core in ``local_runtime.integrations.calls``.
This adds the Action's part: a write is approved through the same preflight and
claim as ``capture_screen``, so one answer runs exactly one write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_string_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import (
    INTEGRATION_RUN_TOOL_TOOL_ID,
    ToolDefinition,
)
from pantaray_agents.local_runtime.integrations.calls import (
    IntegrationRefusal,
    ResolvedIntegrationCall,
    execute_integration_call,
    refusal_details,
    resolve_integration_call,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    ensure_tool_authorization,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.brokering.execution_start import (
    claim_approved_execution_start,
)
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    integration_tool_scope,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.utils.trace_context import get_trace_context

from .approval_preparation import (
    build_approval_denied_preparation,
    build_approval_required_preparation,
)
from .shared import (
    CompletedToolControl,
    FailedToolControl,
    ToolExecutionPreparation,
    UnprojectedToolExecutionResult,
    tool_failure_identity_from_exception,
)

APPROVAL_STATE_CHANGED = "approval_state_changed"


class IntegrationIdentityError(RuntimeError):
    """The Action state does not identify who asks for an integration write."""


@dataclass(frozen=True, slots=True)
class _Identity:
    action_id: str
    execution_session_id: str
    manifest_id: str
    process_id: str
    user_id: str


async def run_integration_run_preflight(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    requested_at: str,
) -> ToolExecutionPreparation:
    """Settle consent before a write. The call itself reports any refusal."""

    try:
        identity = _resolve_identity(state)
    except IntegrationIdentityError as exc:
        return _failed_preparation(step_id, tool_def, requested_at, error=exc)
    resolved = await _resolve(identity.user_id, args)
    if (
        isinstance(resolved, ResolvedIntegrationCall)
        and resolved.tool.tool_class == "write"
    ):
        try:
            _authorize_write(identity, resolved, tool_request_id, requested_at)
        except BrokerApprovalRequiredError as exc:
            db_path, busy_timeout_ms = read_local_runtime_db_config()
            try:
                return build_approval_required_preparation(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    step_id=step_id,
                    tool_def=tool_def,
                    tool_request_id=tool_request_id,
                    approval_session_id=exc.approval_session_id,
                    user_id=identity.user_id,
                    manifest_id=identity.manifest_id,
                    action_id=identity.action_id,
                )
            except BrokerPolicyError as missing:
                return _failed_preparation(
                    step_id, tool_def, requested_at, error=missing
                )
        except BrokerApprovalDeniedError as exc:
            return build_approval_denied_preparation(
                step_id=step_id,
                tool_def=tool_def,
                tool_request_id=tool_request_id,
                approval_session_id=exc.approval_session_id,
                requested_at=requested_at,
            )
        except (BrokerPolicyError, MigrationError) as exc:
            return _failed_preparation(step_id, tool_def, requested_at, error=exc)
    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="success",
            started_at=requested_at,
            completed_at=requested_at,
            output={"status": "authorized"},
        ),
        control=CompletedToolControl(),
    )


async def run_integration_run_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    state,
    args: ToolArgs,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> UnprojectedToolExecutionResult:
    identity = _resolve_identity(state)
    resolved = await _resolve(identity.user_id, args)
    if isinstance(resolved, IntegrationRefusal):
        return _refusal_result(step_id, tool_def, requested_at, resolved)
    if resolved.tool.tool_class == "write":
        try:
            _claim_write(
                identity, resolved, tool_request_id, tool_invocation_id, requested_at
            )
        except (
            BrokerApprovalRequiredError,
            BrokerApprovalDeniedError,
            BrokerPolicyError,
        ) as exc:
            # The approval moved underneath the call: withdrawn, already spent by
            # a replay, or the tool became a write after the preflight.
            message = (
                f"Approval for this write changed before it ran ({type(exc).__name__}),"
                " so nothing ran. Request it again."
            )
            return _error_result(
                step_id,
                tool_def,
                requested_at,
                error_type=APPROVAL_STATE_CHANGED,
                message=message,
            )
    executed = await execute_integration_call(resolved, tool_request_id=tool_request_id)
    if isinstance(executed, IntegrationRefusal):
        return _refusal_result(step_id, tool_def, requested_at, executed)
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=requested_at,
        completed_at=now_utc_iso(),
        output=executed,
    )


async def _resolve(
    user_id: str, args: ToolArgs
) -> ResolvedIntegrationCall | IntegrationRefusal:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return await resolve_integration_call(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        slug=require_string_arg(args, "slug"),
        account=optional_string_arg(args, "account"),
        arguments=require_string_arg(args, "arguments"),
        read_only=False,
    )


def _authorize_write(
    identity: _Identity,
    resolved: ResolvedIntegrationCall,
    tool_request_id: str,
    requested_at: str,
) -> tuple[str | None, Literal["settings", "prompt"] | None]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return ensure_tool_authorization(
        context=load_broker_context(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=INTEGRATION_RUN_TOOL_TOOL_ID,
            path_access_kind="none",
            user_id=identity.user_id,
            actor_process_id=identity.process_id,
            manifest_id=identity.manifest_id,
            execution_session_id=identity.execution_session_id,
        ),
        tool_invocation_id=None,
        tool_request_id=tool_request_id,
        # The approval binds to this summary: a request that resolves to another
        # toolkit, account, slug or arguments no longer matches it and is refused.
        # No tool has its own summary, so the user sees the whole request.
        command_summary={
            "summary_kind": "integration_tool",
            "account": resolved.account.label,
            "toolkit": resolved.toolkit_name,
            "tool": resolved.tool.name,
            "description": resolved.tool.description,
            "arguments": json.dumps(
                resolved.arguments, ensure_ascii=False, indent=2, sort_keys=True
            ),
            "toolkit_id": resolved.tool.toolkit,
            "account_id": resolved.account.account,
            "slug": resolved.tool.slug,
        },
        requested_at=requested_at,
        integration_tool_scope=integration_tool_scope(
            account=resolved.account.account, slug=resolved.tool.slug
        ),
    )


def _claim_write(
    identity: _Identity,
    resolved: ResolvedIntegrationCall,
    tool_request_id: str,
    tool_invocation_id: str,
    requested_at: str,
) -> None:
    """Spend the user's approval so that one answer runs exactly one write."""

    session_id, source = _authorize_write(
        identity, resolved, tool_request_id, requested_at
    )
    if source is None:
        raise BrokerPolicyError("an integration write was authorized without a prompt")
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    claim_approved_execution_start(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=identity.user_id,
        tool_request_id=tool_request_id,
        action_id=identity.action_id,
        approval_session_id=session_id,
        approval_source=source,
        tool_invocation_id=tool_invocation_id,
        # The Action runtime records this tool's invocation row before execution.
        invocation=None,
        started_at=requested_at,
    )


def _resolve_identity(state) -> _Identity:
    trace = get_trace_context()
    values = {
        "action_id": state.get("action_id"),
        "execution_session_id": state.get("execution_session_id"),
        "manifest_id": state.get("manifest_id"),
        "process_id": trace.extra.get("process_id") if trace is not None else None,
        "user_id": state.get("user_id"),
    }
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise IntegrationIdentityError(f"{name} is required for integration tools")
    return _Identity(**{name: str(value) for name, value in values.items()})


def _refusal_result(
    step_id: str,
    tool_def: ToolDefinition,
    requested_at: str,
    refusal: IntegrationRefusal,
) -> UnprojectedToolExecutionResult:
    return _error_result(
        step_id,
        tool_def,
        requested_at,
        error_type=refusal.error.kind,
        message=refusal.message,
        details=refusal_details(refusal),
    )


def _error_result(
    step_id: str,
    tool_def: ToolDefinition,
    requested_at: str,
    *,
    error_type: str,
    message: str,
    details: dict[str, JSONValue] | None = None,
) -> UnprojectedToolExecutionResult:
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="error",
        started_at=requested_at,
        completed_at=now_utc_iso(),
        output=build_runtime_tool_error_output(
            error_type=error_type, message=message, details=details
        ),
    )


def _failed_preparation(
    step_id: str, tool_def: ToolDefinition, requested_at: str, *, error: Exception
) -> ToolExecutionPreparation:
    return ToolExecutionPreparation(
        result=_error_result(
            step_id,
            tool_def,
            requested_at,
            error_type=error.__class__.__name__,
            message=str(error),
        ),
        control=FailedToolControl(failure=tool_failure_identity_from_exception(error)),
    )

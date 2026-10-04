"""Brokered workspace tool execution wrappers."""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.agents_md import (
    attach_repository_agents_md,
)
from pantaray_agents.agents.action_agent.runtime.tool_attachments import (
    coerce_tool_attachments,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalDeniedError,
    BrokerApprovalRequiredError,
    BrokerCompletionPersistenceError,
    BrokerExecutionError,
    BrokerPolicyError,
    FinalizedBrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    READ_TOOL_ID,
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    BrokerPreflightOutcome,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import get_trace_context

from .approval_preparation import (
    build_approval_denied_preparation,
    build_approval_required_preparation,
)
from .shared import (
    CompletedToolControl,
    FailedToolControl,
    ToolCompletionAuditPersistenceError,
    ToolExecutionPreparation,
    ToolFailureIdentity,
    UnprojectedToolExecutionResult,
    tool_failure_identity_from_exception,
)


def _build_error_preparation(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    error: BaseException,
) -> ToolExecutionPreparation:
    completed_at = now_utc_iso()
    effective_error = (
        error.cause if isinstance(error, FinalizedBrokerPolicyError) else error
    )
    error_payload: dict[str, JSONValue] = {
        "type": effective_error.__class__.__name__,
        "message": str(effective_error),
    }
    code = getattr(effective_error, "code", None)
    if isinstance(code, str) and code:
        error_payload["code"] = code
    fix_hint = getattr(effective_error, "fix_hint", None)
    if isinstance(fix_hint, str) and fix_hint:
        error_payload["fix_hint"] = fix_hint
    examples = getattr(effective_error, "examples", None)
    if isinstance(examples, tuple) and all(isinstance(item, str) for item in examples):
        error_payload["examples"] = list(examples)
    raw_output: dict[str, JSONValue] = {"error": error_payload}
    finalized_output: FinalizedToolOutput | None = None
    tool_invocation_id = getattr(error, "tool_invocation_id", None)
    if isinstance(error, (BrokerExecutionError, FinalizedBrokerPolicyError)):
        if error.finalized_output is not None and error.output_storage_kind is not None:
            finalized_output = FinalizedToolOutput(
                output=error.finalized_output,
                storage_kind=error.output_storage_kind,
                owner_kind="tool_invocation",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
            )
    result = UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="error",
        started_at=completed_at,
        completed_at=completed_at,
        output=raw_output,
        tool_invocation_id=(
            tool_invocation_id if isinstance(tool_invocation_id, str) else None
        ),
    )
    return ToolExecutionPreparation(
        result=result,
        control=FailedToolControl(
            failure=tool_failure_identity_from_exception(effective_error)
        ),
        finalized_output=finalized_output,
    )


async def run_broker_tool_wrapper(
    agent,
    step_id: str,
    tool_def: ToolDefinition,
    args,
    state,
    *,
    invocation_id: str | None,
    tool_request_id: str,
    requested_at: str,
    preflight_only: bool,
) -> ToolExecutionPreparation:
    manifest_id = state.get("manifest_id")
    execution_session_id = state.get("execution_session_id")
    user_id = state.get("user_id")
    trace = get_trace_context()
    actor_process_id = trace.extra.get("process_id") if trace is not None else None
    if not isinstance(manifest_id, str) or not manifest_id:
        return _build_error_preparation(
            step_id=step_id,
            tool_def=tool_def,
            error=BrokerPolicyError("manifest_id is required for brokered tools"),
        )
    if not isinstance(execution_session_id, str) or not execution_session_id:
        return _build_error_preparation(
            step_id=step_id,
            tool_def=tool_def,
            error=BrokerPolicyError(
                "execution_session_id is required for brokered tools"
            ),
        )
    if not isinstance(user_id, str) or not user_id:
        return _build_error_preparation(
            step_id=step_id,
            tool_def=tool_def,
            error=BrokerPolicyError("user_id is required for brokered tools"),
        )
    if not isinstance(actor_process_id, str) or not actor_process_id:
        return _build_error_preparation(
            step_id=step_id,
            tool_def=tool_def,
            error=BrokerPolicyError(
                "actor process identity is required for brokered tools"
            ),
        )

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        outcome = await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            tool_id=tool_def.tool_id,
            user_id=user_id,
            actor_process_id=actor_process_id,
            manifest_id=manifest_id,
            execution_session_id=execution_session_id,
            args=dict(args),
            invocation_id=invocation_id,
            tool_request_id=tool_request_id,
            requested_at=requested_at,
            preflight_only=preflight_only,
        )
    except BrokerApprovalRequiredError as exc:
        action_id = state.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return _build_error_preparation(
                step_id=step_id,
                tool_def=tool_def,
                error=BrokerPolicyError("action_id is required for brokered tools"),
            )
        try:
            return build_approval_required_preparation(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                step_id=step_id,
                tool_def=tool_def,
                tool_request_id=tool_request_id,
                approval_session_id=exc.approval_session_id,
                user_id=user_id,
                manifest_id=manifest_id,
                action_id=action_id,
            )
        except BrokerPolicyError as missing_session:
            return _build_error_preparation(
                step_id=step_id,
                tool_def=tool_def,
                error=missing_session,
            )
    except BrokerApprovalDeniedError as exc:
        return build_approval_denied_preparation(
            step_id=step_id,
            tool_def=tool_def,
            tool_request_id=tool_request_id,
            approval_session_id=exc.approval_session_id,
            requested_at=requested_at,
        )
    except BrokerCompletionPersistenceError as exc:
        raise ToolCompletionAuditPersistenceError(
            tool_id=tool_def.tool_id,
            tool_invocation_id=exc.tool_invocation_id,
        ) from exc
    except (BrokerPolicyError, BrokerExecutionError) as exc:
        return _build_error_preparation(step_id=step_id, tool_def=tool_def, error=exc)

    completed_at = now_utc_iso()
    if isinstance(outcome, BrokerPreflightOutcome):
        return ToolExecutionPreparation(
            result=UnprojectedToolExecutionResult(
                step_id=step_id,
                tool_id=tool_def.tool_id,
                status=outcome.status,
                started_at=requested_at,
                completed_at=completed_at,
                output=outcome.output,
            ),
            control=CompletedToolControl(),
        )
    failure_control = (
        FailedToolControl(
            failure=ToolFailureIdentity(
                error_type=outcome.failure.error_type,
                message=outcome.failure.message,
            )
        )
        if outcome.failure is not None
        else CompletedToolControl()
    )
    agents_md: str | None = None
    if outcome.status == "success" and outcome.failure is None:
        try:
            read_context = load_broker_context(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                tool_id=READ_TOOL_ID,
                path_access_kind="read",
                user_id=user_id,
                actor_process_id=actor_process_id,
                manifest_id=manifest_id,
                execution_session_id=execution_session_id,
            )
        except BrokerPolicyError:
            # An Action that may not read files gets no AGENTS.md either.
            read_context = None
        if read_context is not None:
            agents_md = attach_repository_agents_md(
                state, tool_id=tool_def.tool_id, args=args, read_context=read_context
            )
    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status=outcome.status,
            started_at=completed_at,
            completed_at=completed_at,
            output=outcome.output,
            attachments=coerce_tool_attachments(outcome.attachments),
            tool_invocation_id=outcome.tool_invocation_id,
            agents_md=agents_md,
        ),
        control=failure_control,
        finalized_output=FinalizedToolOutput(
            output=outcome.output,
            storage_kind=outcome.output_storage_kind,
            owner_kind="tool_invocation",
            search_text=outcome.search_text,
            stdout_text=outcome.stdout_text,
            stderr_text=outcome.stderr_text,
        ),
    )

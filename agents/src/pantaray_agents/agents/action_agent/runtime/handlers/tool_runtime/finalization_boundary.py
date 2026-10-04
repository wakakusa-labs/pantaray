"""Action-owned adapter for the canonical local tool-result finalizer."""

from __future__ import annotations

import logging
from typing import Literal, cast

from pantaray_agents.agents.action_agent.runtime.log_safety import (
    exception_type_name,
    safe_exception_origin,
    safe_mapping_keys,
    summarize_missing_required_string_fields,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.invocation_audit import (
    start_local_tool_invocation_audit,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    ActionStepToolResultOwner,
    FinalizedToolOutput,
    InvocationToolResultOwner,
    ToolResultCompletionScope,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from pantaray_agents.local_runtime.tooling.tool_result_validation import (
    ToolOutputValidationError,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output
from pantaray_agents.utils.structured_logging import log_structured_event

from .shared import (
    ToolCompletionAuditPersistenceError,
    ToolExecutionPreparation,
    ToolExecutionResult,
    ToolExecutionStatus,
    ToolValidationError,
    UnprojectedToolExecutionResult,
    ValidatedToolArgs,
    build_tool_execution_result,
)

logger = logging.getLogger(__name__)
_LOCAL_AUDIT_REQUIRED_STATE_FIELDS = (
    "manifest_id",
    "execution_session_id",
    "execution_network_policy",
    "action_id",
    "user_id",
)


class ActionStepResultFinalizationError(RuntimeError):
    """A synthetic Action-step result could not be durably projected."""

    def __init__(self, *, step_id: str) -> None:
        super().__init__("Action step result could not be durably projected.")
        self.step_id = step_id


def resolve_local_audit_context(
    state: ActionAgentState,
) -> tuple[str, str, str, str, str]:
    values = tuple(state.get(key) for key in _LOCAL_AUDIT_REQUIRED_STATE_FIELDS)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise RuntimeError(
            "local tool execution requires manifest_id, execution_session_id, "
            "execution_network_policy, action_id, and user_id"
        )
    manifest_id, session_id, network_policy, action_id, user_id = values
    assert isinstance(manifest_id, str)
    assert isinstance(session_id, str)
    assert isinstance(network_policy, str)
    assert isinstance(action_id, str)
    assert isinstance(user_id, str)
    return manifest_id, session_id, network_policy, action_id, user_id


def start_action_tool_invocation(
    *,
    state: ActionAgentState,
    tool_def: ToolDefinition,
    step_id: str,
    started_at: str,
    args: ValidatedToolArgs,
    tool_request_id: str | None = None,
) -> str:
    try:
        audit_context = resolve_local_audit_context(state)
    except RuntimeError as exc:
        log_structured_event(
            logger,
            level="error",
            evt="ACTION_LOCAL_TOOL_AUDIT_CONTEXT_MISSING",
            component="action_agent.tool_runtime.finalization_boundary",
            tool_id=tool_def.tool_id,
            phase=str(state.get("phase") or ""),
            missing_fields=cast(
                JSONValue,
                summarize_missing_required_string_fields(
                    state,
                    required_fields=_LOCAL_AUDIT_REQUIRED_STATE_FIELDS,
                ),
            ),
            state_keys=cast(JSONValue, safe_mapping_keys(state)),
            exception_type=exception_type_name(exc),
            exception_origin=safe_exception_origin(exc),
        )
        raise
    manifest_id, session_id, _network_policy, action_id, user_id = audit_context
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return start_local_tool_invocation_audit(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
        action_id=action_id,
        step_id=step_id,
        tool_id=tool_def.tool_id,
        manifest_id=manifest_id,
        execution_session_id=session_id,
        args=dict(args),
        tool_request_id=tool_request_id,
        started_at=started_at,
    )


def finalize_action_tool_invocation(
    *,
    tool_id: str,
    invocation_id: str | None,
    result: UnprojectedToolExecutionResult | None,
    error: BaseException | None = None,
    completion_scope: ToolResultCompletionScope = "execution",
    error_status: Literal["failed", "canceled"] = "failed",
) -> FinalizedToolOutput | None:
    """Close one invocation's audit row.

    `error_status` selects the terminal status recorded for the `error` branch;
    a cancelled execution records `canceled` rather than `failed`.
    """
    if invocation_id is None:
        return None
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        if result is not None:
            search_text, stdout_text, stderr_text = _index_text(result)
            return finalize_local_tool_result(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                request=ToolResultFinalizationRequest(
                    owner=InvocationToolResultOwner(
                        invocation_id=invocation_id,
                        completed_at=result.completed_at,
                        status=_invocation_terminal_status(result.status),
                        completion_scope=completion_scope,
                    ),
                    output=result.output,
                    search_text=search_text,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                ),
            )
        assert error is not None
        details = error.details if isinstance(error, ToolValidationError) else None
        return finalize_local_tool_result(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ToolResultFinalizationRequest(
                owner=InvocationToolResultOwner(
                    invocation_id=invocation_id,
                    completed_at=now_utc_iso(),
                    status=error_status,
                    completion_scope=completion_scope,
                ),
                output=build_runtime_tool_error_output(
                    error_type=error.__class__.__name__,
                    message=str(error),
                    details=details,
                ),
            ),
        )
    except ToolOutputValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        log_structured_event(
            logger,
            level="error",
            evt="ACTION_TOOL_COMPLETION_AUDIT_PERSISTENCE_FAILED",
            component="action_agent.tool_runtime.finalization_boundary",
            tool_id=tool_id,
            tool_invocation_id=invocation_id,
            exception_type=exception_type_name(exc),
            exception_origin=safe_exception_origin(exc),
        )
        raise ToolCompletionAuditPersistenceError(
            tool_id=tool_id,
            tool_invocation_id=invocation_id,
        ) from exc


def finalize_action_step_preparation(
    *,
    state: ActionAgentState,
    preparation: ToolExecutionPreparation,
) -> ToolExecutionResult:
    finalized = preparation.finalized_output or _finalize_action_step_output(
        state=state,
        step_id=preparation.result.step_id,
        result=preparation.result,
    )
    return build_tool_execution_result(
        preparation.result,
        finalized_output=finalized,
        control=preparation.control,
    )


def build_validation_error_output(
    error: ToolValidationError,
) -> dict[str, JSONValue]:
    return build_runtime_tool_error_output(
        error_type=error.__class__.__name__,
        message=str(error),
        details=error.details,
    )


def _finalize_action_step_output(
    *,
    state: ActionAgentState,
    step_id: str,
    result: UnprojectedToolExecutionResult,
) -> FinalizedToolOutput:
    manifest_id, _session_id, _network_policy, action_id, user_id = (
        resolve_local_audit_context(state)
    )
    return finalize_action_step_output(
        manifest_id=manifest_id,
        action_id=action_id,
        user_id=user_id,
        step_id=step_id,
        output=result.output,
    )


def finalize_action_step_output(
    *,
    manifest_id: str,
    action_id: str,
    user_id: str,
    step_id: str,
    output: object,
) -> FinalizedToolOutput:
    """Durably project one synthetic formal-step output under its step ID."""

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return finalize_local_tool_result(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ToolResultFinalizationRequest(
                owner=ActionStepToolResultOwner(
                    manifest_id=manifest_id,
                    action_id=action_id,
                    user_id=user_id,
                    step_id=step_id,
                ),
                output=output,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log_structured_event(
            logger,
            level="error",
            evt="ACTION_STEP_RESULT_FINALIZATION_FAILED",
            component="action_agent.tool_runtime.finalization_boundary",
            step_id=step_id,
            exception_type=exception_type_name(exc),
            exception_origin=safe_exception_origin(exc),
        )
        raise ActionStepResultFinalizationError(step_id=step_id) from exc


def _index_text(
    result: UnprojectedToolExecutionResult,
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(result.output, dict):
        return None, None, None
    search = result.output.get("content")
    stdout = result.output.get("stdout")
    stderr = result.output.get("stderr")
    return (
        search if isinstance(search, str) else None,
        stdout if isinstance(stdout, str) else None,
        stderr if isinstance(stderr, str) else None,
    )


def _invocation_terminal_status(
    status: ToolExecutionStatus,
) -> Literal["completed", "failed"]:
    if status == "success":
        return "completed"
    if status == "error":
        return "failed"
    raise ValueError("processing tool results do not own terminal invocations")


__all__ = [
    "ActionStepResultFinalizationError",
    "build_validation_error_output",
    "finalize_action_step_output",
    "finalize_action_step_preparation",
    "finalize_action_tool_invocation",
    "resolve_local_audit_context",
    "start_action_tool_invocation",
]

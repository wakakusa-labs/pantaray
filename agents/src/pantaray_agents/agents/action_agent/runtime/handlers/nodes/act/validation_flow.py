"""Validation-error handling for act step."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.state.context import (
    ensure_context as _ensure_context,
)
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    FinalizedToolStepResult,
    TerminalStepEmission,
    record_tool_step,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from ...tool_runtime.formal_step import finalize_error_step, finalize_synthetic_step
from ...tool_runtime.outcome_line import build_outcome_line
from ...tools import ToolValidationError
from .. import common
from .builders import _build_tool_history_entry
from .call_slots import ToolCallSlot
from .helpers import (
    _apply_tool_result_state,
    _extract_previous_step_note,
    _increment_tool_validation_error_streak,
    _maybe_abort_validation_retries,
    _record_validation_error,
    _resolve_parent_step_id,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
    from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.state_access import (  # noqa: E501
        ToolArgsPayload,
    )
    from pantaray_agents.agents.action_agent.tools import ToolDefinition


type ValidationErrorDetails = dict[str, JSONValue]


def _validation_error_invocation_ids(
    prior_invocation_ids: tuple[str, ...],
    exc: ToolValidationError,
) -> tuple[str, ...]:
    if exc.tool_invocation_id is None:
        return prior_invocation_ids
    if exc.tool_invocation_id in prior_invocation_ids:
        return prior_invocation_ids
    return (*prior_invocation_ids, exc.tool_invocation_id)


async def _save_pre_run_validation_error_step(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
    *,
    label: str,
    step_id: str,
    tool_id: str,
    args_payload: ToolArgsPayload,
    result: FinalizedToolStepResult,
    started_at: str,
    completed_at: str,
    history_step_number: int,
    goal_handle: str,
    short_step_id: str,
    local_step_number: int,
) -> None:
    parent_step_id = _resolve_parent_step_id(state)
    await record_tool_step(
        agent,
        state,
        terminal_emission=TerminalStepEmission(runtime.emit_action_step, label),
        step_id=step_id,
        action_id=state["action_id"],
        step_number=history_step_number,
        step_name=f"tool::{tool_id}",
        tool_args={"tool_id": tool_id, "args": args_payload},
        result=result,
        started_at=started_at,
        completed_at=completed_at,
        retry_count=0,
        parent_step_id=parent_step_id,
        goal_handle=goal_handle,
        user_id=str(state.get("user_id") or ""),
        short_step_id=short_step_id,
        local_step_number=local_step_number,
        tool_invocation_ids=(),
    )


async def handle_malformed_next_action(
    agent: ActionAgent,
    runtime: ActionGraphRuntime,
    state: ActionAgentState,
    *,
    raw_next_action: object,
    tools_by_id: Mapping[str, ToolDefinition],
    allowed_tool_ids: tuple[str, ...],
) -> ActionAgentState:
    """canonical な next_action へ落とせない checkpoint 値を repair loop へ戻す。"""

    if isinstance(raw_next_action, dict):
        raw_tool = raw_next_action.get("tool")
        if isinstance(raw_tool, dict):
            raw_tool_id = str(raw_tool.get("tool_id", "") or "").strip()
            raw_tool_def = tools_by_id.get(raw_tool_id) if raw_tool_id else None
            raw_args = raw_tool.get("args")
            if raw_tool_def is not None and not isinstance(raw_args, dict):
                return await handle_invalid_tool_args_shape(
                    agent,
                    runtime,
                    state,
                    tool_id=raw_tool_id,
                    tool_def=raw_tool_def,
                    started_at=now_utc_iso(),
                )
    return await handle_invalid_tool_call(
        agent,
        runtime,
        state,
        tool_id="invalid_tool_call",
        malformed_exc=ToolValidationError(
            "next_action must be a NextActionModel.",
            details={
                "path": ["next_action"],
                "message": "Expected canonical next_action object.",
            },
        ),
        allowed_tool_ids=allowed_tool_ids,
    )


async def handle_invalid_tool_call(
    agent: ActionAgent,
    runtime: ActionGraphRuntime,
    state: ActionAgentState,
    *,
    tool_id: str,
    malformed_exc: ToolValidationError | None,
    allowed_tool_ids: tuple[str, ...],
) -> ActionAgentState:
    context = _ensure_context(state)
    streak = _increment_tool_validation_error_streak(context)
    started_at = now_utc_iso()
    completed_at = now_utc_iso()
    error_step_id = str(uuid.uuid4())

    validation_path: list[JSONValue] = ["next_action", "tool", "tool_id"]
    allowed_tool_id_values: list[JSONValue] = []
    allowed_tool_id_values.extend(sorted(allowed_tool_ids))
    default_details: dict[str, JSONValue] = {
        "path": validation_path,
        "message": "tool_id must be one of the supported tool ids.",
        "metadata": {"allowed_tool_ids": allowed_tool_id_values},
    }
    validation_exc = malformed_exc or ToolValidationError(
        f"Unknown tool_id: '{tool_id}'.",
        details=default_details,
    )

    error_details: ValidationErrorDetails = {"tool_id": tool_id}
    if getattr(validation_exc, "details", None):
        error_details["validation"] = dict(validation_exc.details)
    validation_error = _record_validation_error(
        runtime,
        state,
        error_code="ACTION_TOOL_CALL_INVALID",
        error_message=str(validation_exc),
        error_details=error_details,
    )

    error_payload = common._build_tool_error_payload(
        validation_exc,
        attempt=1,
        max_attempts=1,
        traceback_text="",
    )
    inference = common.infer_tool_short_step_id_from_previous_supervisor_think(
        state,
        default_scope_handle=common.SUPERVISOR_SCOPE_HANDLE,
    )
    formal_result = finalize_synthetic_step(
        state,
        step_id=error_step_id,
        status="error",
        output={"error": error_payload, "tool_id": tool_id},
        error=validation_error,
    )
    history_entry = _build_tool_history_entry(
        step_id=error_step_id,
        step_number=state["step"],
        phase=state["phase"],
        summary=_extract_previous_step_note(state),
        tool_id=str(tool_id),
        started_at=started_at,
        completed_at=completed_at,
        result_line=build_outcome_line(
            str(tool_id),
            formal_result.output.output,
            status="failed",
            error_code=validation_error.error_code,
        ),
        output=formal_result.output.output,
        short_step_id=inference.short_step_id,
    )
    common.append_history_entry(
        state,
        scope_handle=inference.scope_handle,
        entry=history_entry,
    )
    _apply_tool_result_state(
        state,
        completed_at=completed_at,
        reset_validation_streak=False,
    )
    await _maybe_abort_validation_retries(
        runtime,
        state,
        tool_id=tool_id,
        streak=streak,
        error_code="ACTION_TOOL_CALL_INVALID_RETRY_EXCEEDED",
        error_message=(
            "Tool call validation failed repeatedly; aborting supervisor retries."
        ),
    )
    await _save_pre_run_validation_error_step(
        agent,
        state,
        runtime,
        label=tool_id,
        step_id=error_step_id,
        tool_id=str(tool_id),
        args_payload={},
        result=formal_result,
        started_at=started_at,
        completed_at=completed_at,
        history_step_number=history_entry["step_number"],
        goal_handle=inference.scope_handle,
        short_step_id=inference.short_step_id,
        local_step_number=inference.local_step_number,
    )
    return state


async def handle_invalid_tool_args_shape(
    agent: ActionAgent,
    runtime: ActionGraphRuntime,
    state: ActionAgentState,
    *,
    tool_id: str,
    tool_def: ToolDefinition,
    started_at: str,
) -> ActionAgentState:
    malformed_args = ToolValidationError(
        "next_action.tool.args must be an object (dict).",
        details={
            "path": ["next_action", "tool", "args"],
            "message": "Expected object (dict).",
        },
    )
    context = _ensure_context(state)
    streak = _increment_tool_validation_error_streak(context)
    completed_at = now_utc_iso()
    error_step_id = str(uuid.uuid4())
    error_payload = common._build_tool_error_payload(
        malformed_args,
        attempt=1,
        max_attempts=1,
        traceback_text="",
    )
    validation_error = _record_validation_error(
        runtime,
        state,
        error_code="ACTION_TOOL_CALL_INVALID",
        error_message=str(malformed_args),
        error_details={
            "tool_id": tool_id,
            "validation": dict(malformed_args.details or {}),
        },
    )
    inference = common.infer_tool_short_step_id_from_previous_supervisor_think(
        state,
        default_scope_handle=common.SUPERVISOR_SCOPE_HANDLE,
    )
    formal_result = finalize_synthetic_step(
        state,
        step_id=error_step_id,
        status="error",
        output={"error": error_payload, "tool_id": tool_def.tool_id},
        error=validation_error,
    )
    history_entry = _build_tool_history_entry(
        step_id=error_step_id,
        step_number=state["step"],
        phase=state["phase"],
        summary=_extract_previous_step_note(state),
        tool_id=tool_def.tool_id,
        started_at=started_at,
        completed_at=completed_at,
        result_line=build_outcome_line(
            tool_def.tool_id,
            formal_result.output.output,
            status="failed",
            error_code=validation_error.error_code,
        ),
        output=formal_result.output.output,
        short_step_id=inference.short_step_id,
    )
    common.append_history_entry(
        state,
        scope_handle=inference.scope_handle,
        entry=history_entry,
    )
    _apply_tool_result_state(
        state,
        completed_at=completed_at,
        reset_validation_streak=False,
    )
    await _maybe_abort_validation_retries(
        runtime,
        state,
        tool_id=tool_id,
        streak=streak,
        error_code="ACTION_TOOL_CALL_INVALID_RETRY_EXCEEDED",
        error_message=(
            "Tool call validation failed repeatedly; aborting supervisor retries."
        ),
    )
    await _save_pre_run_validation_error_step(
        agent,
        state,
        runtime,
        label=tool_def.name,
        step_id=error_step_id,
        tool_id=tool_def.tool_id,
        args_payload={},
        result=formal_result,
        started_at=started_at,
        completed_at=completed_at,
        history_step_number=history_entry["step_number"],
        goal_handle=inference.scope_handle,
        short_step_id=inference.short_step_id,
        local_step_number=inference.local_step_number,
    )
    return state


async def handle_tool_validation_error(
    agent: ActionAgent,
    runtime: ActionGraphRuntime,
    state: ActionAgentState,
    *,
    tool_id: str,
    tool_def: ToolDefinition,
    args_payload: ToolArgsPayload,
    started_at: str,
    exc: ToolValidationError,
    step_id: str,
    slot: ToolCallSlot,
    prior_tool_invocation_ids: tuple[str, ...] = (),
) -> ActionAgentState:
    error_details: ValidationErrorDetails = {"tool_id": tool_id}
    if getattr(exc, "details", None):
        error_details["validation"] = dict(exc.details)
    validation_error = _record_validation_error(
        runtime,
        state,
        error_code="ACTION_TOOL_ARGS_INVALID",
        error_message=str(exc),
        error_details=error_details,
    )

    context = _ensure_context(state)
    streak = _increment_tool_validation_error_streak(context)
    completed_at = now_utc_iso()
    error_step_id = step_id
    error_payload = common._build_tool_error_payload(
        exc,
        attempt=1,
        max_attempts=1,
        traceback_text="",
    )
    finalized_error = common.finalized_tool_error(exc)
    formal_result = finalize_error_step(
        state,
        step_id=error_step_id,
        finalized_output=finalized_error,
        raw_output={"error": error_payload, "tool_id": tool_def.tool_id},
        error=validation_error,
    )

    history_entry = _build_tool_history_entry(
        step_id=error_step_id,
        step_number=slot.step_number,
        phase=state["phase"],
        summary=slot.step_note,
        tool_id=tool_def.tool_id,
        started_at=started_at,
        completed_at=completed_at,
        result_line=build_outcome_line(
            tool_def.tool_id,
            formal_result.output.output,
            status="failed",
            error_code=validation_error.error_code,
        ),
        output=formal_result.output.output,
        short_step_id=slot.short_step_id,
        origin=slot.origin,
    )
    common.append_history_entry(
        state,
        scope_handle=slot.scope_handle,
        entry=history_entry,
    )

    _apply_tool_result_state(
        state,
        completed_at=completed_at,
        reset_validation_streak=False,
    )
    parent_step_id = _resolve_parent_step_id(state)
    await _maybe_abort_validation_retries(
        runtime,
        state,
        tool_id=tool_id,
        streak=streak,
        error_code="ACTION_TOOL_ARGS_INVALID_RETRY_EXCEEDED",
        error_message=(
            "Tool args validation failed repeatedly; aborting supervisor retries."
        ),
    )
    await record_tool_step(
        agent,
        state,
        terminal_emission=TerminalStepEmission(runtime.emit_action_step, tool_def.name),
        step_id=error_step_id,
        action_id=state["action_id"],
        step_number=slot.step_number,
        step_name=f"tool::{tool_def.tool_id}",
        tool_args={"tool_id": tool_def.tool_id, "args": args_payload},
        result=formal_result,
        started_at=started_at,
        completed_at=completed_at,
        retry_count=0,
        parent_step_id=parent_step_id,
        goal_handle=slot.scope_handle,
        user_id=str(state.get("user_id") or ""),
        short_step_id=slot.short_step_id,
        local_step_number=slot.local_step_number,
        tool_invocation_ids=_validation_error_invocation_ids(
            prior_tool_invocation_ids,
            exc,
        ),
        origin=slot.origin,
    )
    return state

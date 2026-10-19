"""Action ノードの共通ヘルパー。"""

# pylint: disable=protected-access

from __future__ import annotations

from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.error_redaction import (
    emit_redacted_agent_error,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentContext,
    ActionAgentState,
    ActionPhase,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    ensure_context as _ensure_context,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    increment_tool_steps_taken,
)
from pantaray_agents.agents.action_agent.tools import (
    ToolDefinition,
    select_supervisor_act_tool_ids,
    select_supervisor_act_tool_registry,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import AgentError, JSONValue

from ...tool_runtime.shared import ToolExecutionActor
from .. import common

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime


# Supervisor が tool 実行前の JSON Schema 検証に失敗した場合（ToolValidationError）に、
# LLM が自己修復するための再推論ループを許可する。ただし無限ループ防止のため上限を設ける。
_MAX_TOOL_VALIDATION_ERROR_STREAK = 5

type ValidationErrorDetails = dict[str, JSONValue]


def _resolve_act_tool_contract(
    state: ActionAgentState,
) -> tuple[dict[str, ToolDefinition], tuple[str, ...], ToolExecutionActor]:
    """Resolve tools and actor identity from the durable lifecycle phase."""

    phase = state.get("phase")
    if phase == "executing":
        integrations = _ensure_context(state).get("integration_tools_exposed", False)
        return (
            select_supervisor_act_tool_registry(integrations=integrations),
            select_supervisor_act_tool_ids(integrations=integrations),
            "supervisor",
        )
    raise ValueError(
        f"Action execution is available only while executing; got phase={phase!r}."
    )


def _phase_after_tool_execution(state: ActionAgentState) -> ActionPhase:
    """Return the lifecycle phase committed by a completed tool execution."""

    phase = state.get("phase")
    if phase == "executing":
        return "executing"
    raise ValueError(
        "Tool execution cannot advance an inactive Action lifecycle; "
        f"got phase={phase!r}."
    )


def _increment_tool_validation_error_streak(context: ActionAgentContext) -> int:
    """tool validation error の連続回数を更新して返す。"""
    prev_streak = context.get("tool_validation_error_streak", 0)
    streak = int(prev_streak or 0) + 1
    context["tool_validation_error_streak"] = streak
    return streak


def _record_validation_error(
    runtime: ActionGraphRuntime,
    state: ActionAgentState,
    *,
    error_code: str,
    error_message: str,
    error_details: ValidationErrorDetails,
) -> AgentError:
    """Record a recoverable validation error for the agent repair loop."""
    validation_error = runtime.services.response.build_agent_error(
        error_type="validation_error",
        error_code=error_code,
        error_message=error_message,
        severity="warning",
        error_details=error_details,
    )
    append_state_error(state, error=validation_error)
    return validation_error


async def _maybe_abort_validation_retries(
    runtime: ActionGraphRuntime,
    state: ActionAgentState,
    *,
    tool_id: str,
    streak: int,
    error_code: str,
    error_message: str,
) -> None:
    """validation error が連続した場合に打ち切る。"""
    if streak < _MAX_TOOL_VALIDATION_ERROR_STREAK:
        return
    exceeded = runtime.services.response.build_agent_error(
        error_type="validation_error",
        error_code=error_code,
        error_message=error_message,
        error_details={
            "tool_id": tool_id,
            "attempts": streak,
            "max_attempts": _MAX_TOOL_VALIDATION_ERROR_STREAK,
        },
    )
    await emit_redacted_agent_error(runtime.emit_error, exceeded)
    append_state_error(state, error=exceeded, prepend=True)
    set_status_with_updated_at(state, status="error")
    state["final_output"] = ""
    state["next_action"] = None


def _apply_tool_result_state(
    state: ActionAgentState,
    *,
    completed_at: str,
    reset_validation_streak: bool,
) -> ActionAgentContext:
    """tool 実行結果を state/context に反映する。"""
    context = _ensure_context(state)
    if reset_validation_streak:
        context["tool_validation_error_streak"] = 0
    state["next_action"] = None
    state["step"] = state["step"] + 1
    # act は Tool ステップとしてカウントする（LLM/Tool の内訳を保持）
    increment_tool_steps_taken(state)
    state["updated_at"] = completed_at
    return context


def _resolve_parent_step_id(state: ActionAgentState) -> str | None:
    """Resolve the preceding planning or execution THINK as the tool parent."""

    phase = common.require_active_action_phase(state)
    history = common.get_history_for_scope(
        state, scope_handle=common.SUPERVISOR_SCOPE_HANDLE
    )
    for entry in reversed(history):
        if (
            entry.get("phase") == phase
            and entry.get("step_type") == StepType.LLM_OUTPUT
        ):
            return entry.get("step_id")
    return None


def _extract_previous_step_note(state: ActionAgentState) -> str:
    """Read the step_note the preceding Supervisor THINK stored as its summary."""

    phase = common.require_active_action_phase(state)
    history_list = common.get_history_for_scope(
        state, scope_handle=common.SUPERVISOR_SCOPE_HANDLE
    )
    for entry in reversed(history_list):
        if (
            entry.get("phase") == phase
            and entry.get("step_type") == StepType.LLM_OUTPUT
        ):
            return entry.get("summary", "")
    return ""


__all__ = [
    "_apply_tool_result_state",
    "_extract_previous_step_note",
    "_increment_tool_validation_error_streak",
    "_maybe_abort_validation_retries",
    "_record_validation_error",
    "_phase_after_tool_execution",
    "_resolve_act_tool_contract",
    "_resolve_parent_step_id",
]

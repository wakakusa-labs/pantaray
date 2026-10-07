"""Approval pause checkpoint persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalRequiredToolControl,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    PendingApprovalRequest,
    build_pending_approval_request,
)
from pantaray_agents.agents.action_agent.runtime.state.updates import (
    append_state_error,
    set_status_with_updated_at,
)
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    build_finalized_tool_step,
    record_tool_step,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.tooling.repository import (
    interrupt_approval_session_for_tool_request,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.base import AgentError, JSONValue

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent


APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_CODE = (
    "ACTION_APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED"
)
APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_MESSAGE = (
    "Failed to persist approval pause checkpoint."
)


class AgentErrorBuilder(Protocol):
    def __call__(
        self,
        *,
        error_type: str,
        error_code: str,
        error_message: str,
        severity: str = "error",
        error_details: dict[str, JSONValue] | None = None,
        metadata: dict[str, JSONValue] | None = None,
    ) -> AgentError: ...


@dataclass(frozen=True, slots=True)
class ApprovalPauseToolStep:
    step_id: str
    action_id: str
    step_number: int
    step_name: str
    tool_id: str
    tool_args: dict[str, JSONValue]
    approval: ApprovalRequiredToolControl
    finalized_output: FinalizedToolOutput
    started_at: str
    completed_at: str
    goal_handle: str
    user_id: str
    short_step_id: str
    local_step_number: int
    parent_step_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    execution_time_ms: int | None = None
    retry_count: int = 0
    tool_invocation_ids: tuple[str, ...] = ()
    # The paused row settles into the same step_id once the call resumes, so the
    # identity has to be on it from the first write.
    origin: ActionToolCallOrigin | None = None


async def persist_approval_pause_checkpoint(
    agent: ActionAgent,
    state: ActionAgentState,
    *,
    owner: Literal["supervisor", "goal_worker"],
    tool_id: str,
    args: dict[str, JSONValue],
    requested_at: str,
    tool_step: ApprovalPauseToolStep,
    build_agent_error: AgentErrorBuilder,
    logger: logging.Logger,
    goal_id: str | None = None,
    thinking: str | None = None,
    thinking_summary: str | None = None,
) -> bool:
    """Install approval pause state and persist it as a resumable checkpoint."""

    _install_pending_approval_request(
        state,
        owner=owner,
        tool_id=tool_id,
        args=args,
        approval=tool_step.approval,
        requested_at=requested_at,
        goal_id=goal_id,
        thinking=thinking,
        thinking_summary=thinking_summary,
    )
    try:
        await record_approval_pause_tool_step(agent, state, tool_step)
    except Exception as exc:  # noqa: BLE001
        _interrupt_approval_session(
            user_id=tool_step.user_id,
            action_id=tool_step.action_id,
            approval=tool_step.approval,
            interrupted_at=tool_step.completed_at,
            logger=logger,
        )
        logger.error(
            "Approval pause checkpoint persistence failed: action_id=%s "
            "step_id=%s tool_id=%s exception_type=%s",
            tool_step.action_id,
            tool_step.step_id,
            tool_id,
            type(exc).__name__,
            exc_info=True,
        )
        _mark_approval_pause_checkpoint_failure(
            state,
            build_agent_error=build_agent_error,
            tool_id=tool_id,
            completed_at=tool_step.completed_at,
        )
        return False
    return True


async def record_approval_pause_tool_step(
    agent: ActionAgent,
    state: ActionAgentState,
    tool_step: ApprovalPauseToolStep,
) -> None:
    await record_tool_step(
        agent,
        state,
        step_id=tool_step.step_id,
        action_id=tool_step.action_id,
        step_number=tool_step.step_number,
        step_name=tool_step.step_name,
        tool_args=tool_step.tool_args,
        result=build_finalized_tool_step(
            status="processing",
            output=tool_step.finalized_output,
        ),
        started_at=tool_step.started_at,
        completed_at=tool_step.completed_at,
        goal_handle=tool_step.goal_handle,
        user_id=tool_step.user_id,
        short_step_id=tool_step.short_step_id,
        local_step_number=tool_step.local_step_number,
        parent_step_id=tool_step.parent_step_id,
        prompt_tokens=tool_step.prompt_tokens,
        completion_tokens=tool_step.completion_tokens,
        execution_time_ms=tool_step.execution_time_ms,
        retry_count=tool_step.retry_count,
        tool_invocation_ids=tool_step.tool_invocation_ids,
        origin=tool_step.origin,
    )


def _install_pending_approval_request(
    state: ActionAgentState,
    *,
    owner: Literal["supervisor", "goal_worker"],
    tool_id: str,
    args: dict[str, JSONValue],
    approval: ApprovalRequiredToolControl,
    requested_at: str,
    goal_id: str | None,
    thinking: str | None,
    thinking_summary: str | None,
) -> None:
    pending_request: PendingApprovalRequest = build_pending_approval_request(
        owner=owner,
        tool_id=tool_id,
        args=args,
        approval_session_id=approval.approval_session_id,
        tool_request_id=approval.tool_request_id,
        requested_at=requested_at,
        intent_class=approval.intent_class,
        command_summary=approval.command_summary,
        goal_id=goal_id,
        thinking=thinking,
        thinking_summary=thinking_summary,
    )
    state["pending_approval_request"] = pending_request
    state["current_approval_blockers"] = [
        *(state.get("current_approval_blockers") or []),
        pending_request,
    ]


def _interrupt_approval_session(
    *,
    user_id: str,
    action_id: str,
    approval: ApprovalRequiredToolControl,
    interrupted_at: str,
    logger: logging.Logger,
) -> None:
    tool_request_id = approval.tool_request_id
    try:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        updated = interrupt_approval_session_for_tool_request(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            action_id=action_id,
            tool_request_id=tool_request_id,
            interrupted_at=interrupted_at,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Approval session interrupt failed after checkpoint persistence failure: "
            "action_id=%s tool_request_id=%s",
            action_id,
            tool_request_id,
        )
        return
    if not updated:
        logger.warning(
            "Approval session interrupt did not update a pending row: "
            "action_id=%s tool_request_id=%s",
            action_id,
            tool_request_id,
        )


def _mark_approval_pause_checkpoint_failure(
    state: ActionAgentState,
    *,
    build_agent_error: AgentErrorBuilder,
    tool_id: str,
    completed_at: str,
) -> None:
    _clear_approval_pause_state(state)
    error = build_agent_error(
        error_type="persistence_error",
        error_code=APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_CODE,
        error_message=APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_MESSAGE,
        error_details={"tool_id": tool_id},
    )
    append_state_error(state, error=error, updated_at=completed_at)
    set_status_with_updated_at(state, status="error", updated_at=completed_at)
    state["final_output"] = ""
    state["next_action"] = None


def _clear_approval_pause_state(state: ActionAgentState) -> None:
    state.pop("pending_approval_request", None)
    state.pop("current_approval_blockers", None)


__all__ = [
    "APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_CODE",
    "ApprovalPauseToolStep",
    "persist_approval_pause_checkpoint",
    "record_approval_pause_tool_step",
]

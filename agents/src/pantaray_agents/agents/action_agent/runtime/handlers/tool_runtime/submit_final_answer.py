"""Supervisor final-answer submit tool runtime."""

from __future__ import annotations

from typing import Literal, TypedDict

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.state import set_status_with_updated_at
from pantaray_agents.agents.action_agent.runtime.state.types import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_draft,
)
from pantaray_agents.local_runtime.runtime.action_final_gate import (
    ActionFinalizationBlockers,
    ActionFinalizationRequest,
    read_action_finalization_blockers,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import get_trace_context


class SubmitFinalAnswerPayload(TypedDict):
    status: Literal["final_answer_submitted"]


def _serialize_submit_final_answer_payload(
    payload: SubmitFinalAnswerPayload,
) -> dict[str, JSONValue]:
    return {"status": payload["status"]}


def _validate_finalization_boundary(state: ActionAgentState) -> None:
    active_goal_ids: list[JSONValue] = []
    pending_goal_ids: list[JSONValue] = []
    unread_goal_ids: list[JSONValue] = []
    for goal_id, conversation in state["goal_conversations"].items():
        if conversation.active_turn is not None:
            active_goal_ids.append(goal_id)
        if conversation.pending_completion is not None:
            pending_goal_ids.append(goal_id)
        if any(
            message.direction == "worker_to_supervisor"
            and message.sequence > conversation.supervisor_read_through_sequence
            for message in conversation.messages
        ):
            unread_goal_ids.append(goal_id)
    if active_goal_ids or pending_goal_ids or unread_goal_ids:
        raise ToolValidationError(
            "submit_final_answer requires all Goal Worker conversations to be settled.",
            details={
                "path": ["state", "goal_conversations"],
                "active_goals": active_goal_ids,
                "pending_completion_goals": pending_goal_ids,
                "unread_worker_message_goals": unread_goal_ids,
            },
        )
    if state.get("pending_approval_request") is not None or state.get(
        "current_approval_blockers"
    ):
        raise ToolValidationError(
            "submit_final_answer cannot run while approval is pending.",
            details={"path": ["state", "pending_approval_request"]},
        )


def _pending_supervisor_draft(state: ActionAgentState) -> str:
    draft = state.get("supervisor_pending_final_answer")
    if not isinstance(draft, str) or not draft.strip():
        raise ToolValidationError(
            "submit_final_answer requires a pending Supervisor final-answer draft.",
            details={
                "path": ["state", "supervisor_pending_final_answer"],
                "message": (
                    "Call draft_final_answer with the final answer text before "
                    "calling submit_final_answer."
                ),
            },
        )
    return draft.strip()


def _finalization_request(state: ActionAgentState) -> ActionFinalizationRequest:
    trace = get_trace_context()
    process_id = trace.extra.get("process_id") if trace is not None else None
    if (
        trace is None
        or trace.user_id != state.get("user_id")
        or trace.action_id != state.get("action_id")
        or not isinstance(trace.local_job_id, str)
        or not trace.local_job_id.strip()
        or not isinstance(process_id, str)
        or not process_id.strip()
    ):
        raise RuntimeError(
            "active Action job trace is required to submit a final answer"
        )
    return ActionFinalizationRequest(
        user_id=state["user_id"],
        action_id=state["action_id"],
        parent_process_id=process_id,
        parent_job_id=trace.local_job_id,
    )


def _finalization_remedies(blockers: ActionFinalizationBlockers) -> list[str]:
    remedies: list[str] = []
    if blockers.live_child_process_ids:
        remedies.append(
            "call wait_subagents for live_child_process_ids, or cancel_subagent, "
            "until every one of them is terminal"
        )
    if blockers.uncollected_child_process_ids:
        remedies.append(
            "call wait_subagents for uncollected_child_process_ids to collect "
            "their results"
        )
    if blockers.pending_approval_session_ids:
        remedies.append(
            "wait until a human approves or denies pending_approval_session_ids"
        )
    if blockers.active_claim_ids:
        remedies.append(
            "settle the child holding each of active_claim_ids, because a claim "
            "is released by the settlement of the child that acquired it"
        )
    return remedies


def _require_settled_action_ownership(state: ActionAgentState) -> None:
    """Refuse a normal final answer while the Action still owns open work.

    The parent terminal transaction keeps the durable barrier for the same
    invariants; reading them here turns an opaque terminal failure into a typed
    error naming the child, approval, or claim the Supervisor must settle first.
    """

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    blockers = read_action_finalization_blockers(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=_finalization_request(state),
    )
    if not blockers.blocked:
        return
    raise ToolValidationError(
        "submit_final_answer cannot finalize while the Action still owns "
        "unsettled work. Resolve every blocker, then submit again: "
        + "; ".join(_finalization_remedies(blockers))
        + ".",
        details={
            "rule": "FINALIZATION_BLOCKED",
            "live_child_process_ids": list(blockers.live_child_process_ids),
            "uncollected_child_process_ids": list(
                blockers.uncollected_child_process_ids
            ),
            "pending_approval_session_ids": list(blockers.pending_approval_session_ids),
            "active_claim_ids": list(blockers.active_claim_ids),
        },
    )


async def run_submit_final_answer_tool(
    _agent: object,
    step_id: str,
    tool_def: ToolDefinition,
    _args: ToolArgs,
    state: ActionAgentState,
    *,
    actor: ToolExecutionActor = "supervisor",
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError(
            "submit_final_answer is only available to the Supervisor.",
            details={
                "path": ["tool_id"],
                "message": "Goal Workers submit completion proposals to the Supervisor.",
                "metadata": {"actor": actor},
            },
        )
    if state.get("status") != "processing":
        raise ToolValidationError(
            "submit_final_answer requires the action to be processing.",
            details={
                "path": ["state", "status"],
                "message": "Only a processing action can submit a final answer.",
                "metadata": {"status": str(state.get("status") or "")},
            },
        )
    if state.get("run_authority") == "superseded" or state.get("skip_persist"):
        raise ToolValidationError(
            "submit_final_answer cannot finalize a non-authoritative run.",
            details={
                "path": ["state", "run_authority"],
                "message": "Only the authoritative persisted Supervisor run may submit.",
                "metadata": {
                    "run_authority": str(state.get("run_authority") or ""),
                    "skip_persist": bool(state.get("skip_persist")),
                },
            },
        )

    _validate_finalization_boundary(state)
    answer = _pending_supervisor_draft(state)
    draft_model = state.get("supervisor_memory_draft")
    if draft_model is None:
        raise ToolValidationError(
            "submit_final_answer requires the validated Supervisor memory draft."
        )
    draft = deserialize_memory_draft(draft_model)
    if len(draft.documents) != 1 or draft.documents[0].content.strip() != answer:
        raise ToolValidationError(
            "Supervisor memory draft differs from the pending final answer."
        )
    _require_settled_action_ownership(state)
    completed_at = now_utc_iso()
    state["final_output"] = answer
    state["next_action"] = None
    state.pop("supervisor_pending_final_answer", None)
    set_status_with_updated_at(state, status="success", updated_at=completed_at)

    payload: SubmitFinalAnswerPayload = {"status": "final_answer_submitted"}
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=completed_at,
        completed_at=completed_at,
        output=_serialize_submit_final_answer_payload(payload),
    )


__all__ = ["run_submit_final_answer_tool"]

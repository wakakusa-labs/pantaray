"""Approval resume の checkpoint state 正規化と pending tool request 復元。"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from pantaray_agents.action_status import (
    ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.request_identity import (
    set_aside_resumed_tool_request,
    set_resumed_tool_request_id,
    set_resumed_tool_step_id,
)
from pantaray_agents.agents.action_agent.runtime.models import (
    PendingApprovalRequestModel,
)
from pantaray_agents.agents.action_agent.runtime.models.tool_call import (
    NextActionModel,
    PendingToolBatchModel,
    ToolCallModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    build_next_action,
    build_tool_call,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    ensure_context,
)
from pantaray_agents.local_runtime.tooling.models import StoredApprovalSession

from .resume_errors import ResumeStateError

_APPROVAL_STATUS_PENDING = "pending"
_APPROVAL_STATUS_APPROVED_ONCE = "approved_once"
_APPROVAL_STATUS_DENIED = "denied"
_APPROVAL_STATUS_INTERRUPTED = "interrupted"

type ApprovalSessionLoader = Callable[[str, str], StoredApprovalSession | None]


def _require_supervisor_owner(parsed_request: PendingApprovalRequestModel) -> None:
    """Goal Worker 所有の承認は退役済み。legacy 行は typed に fail-close させる。"""

    if parsed_request.owner != "supervisor":
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message=(
                f"Unsupported pending_approval_request.owner: {parsed_request.owner!r}"
            ),
        )


def select_approval_resume_target(
    checkpoint_state: ActionAgentState,
    *,
    approval_session_id: str | None,
    tool_request_id: str | None,
) -> None:
    if approval_session_id is None and tool_request_id is None:
        return
    if not approval_session_id or not tool_request_id:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message="approval resume target requires session id and tool request id",
        )
    blockers = checkpoint_state.get("current_approval_blockers") or []
    for blocker in blockers:
        parsed = PendingApprovalRequestModel.model_validate(blocker)
        if (
            parsed.approval_session_id == approval_session_id
            and parsed.tool_request_id == tool_request_id
        ):
            _require_supervisor_owner(parsed)
            checkpoint_state["pending_approval_request"] = parsed
            return
    raise ResumeStateError(
        failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
        message="approval resume target is not a current blocker",
    )


def normalize_approval_resume_state(
    checkpoint_state: ActionAgentState,
    *,
    load_approval_session_by_request: ApprovalSessionLoader,
    now_provider: Callable[[], str],
    paused_tool_step_id: str | None = None,
    resumed_approval_session_id: str | None = None,
) -> ActionAgentState:
    """Apply the decided approval of the paused call to its checkpoint.

    ``resumed_approval_session_id`` is the session the ``tool_approval``
    continuation resumes. Only that continuation may restore an ``interrupted``
    session: a USER message sent instead of answering interrupts it and enqueues
    this resume, and the call is then settled as not run. Every other writer of
    ``interrupted`` ends the run, so it never resumes from here.
    """

    pending_request = checkpoint_state.get("pending_approval_request")
    if pending_request is None:
        return checkpoint_state
    try:
        parsed_request = PendingApprovalRequestModel.model_validate(pending_request)
    except ValidationError as exc:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message=f"Invalid pending approval request in checkpoint: {exc}",
        ) from exc
    _require_supervisor_owner(parsed_request)
    user_id = checkpoint_state.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message="approval resume requires user_id in checkpoint state",
        )
    approval_session = load_approval_session_by_request(
        user_id,
        parsed_request.tool_request_id,
    )
    if approval_session is None:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message="Approval session not found for pending tool request",
        )
    if approval_session.approval_session_id != parsed_request.approval_session_id:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message="Approval session locator does not match pending tool request",
        )
    approval_status = getattr(approval_session, "status", None)
    if approval_status == _APPROVAL_STATUS_PENDING:
        return checkpoint_state
    set_aside = (
        approval_status == _APPROVAL_STATUS_INTERRUPTED
        and resumed_approval_session_id == parsed_request.approval_session_id
    )
    if approval_status == _APPROVAL_STATUS_DENIED or set_aside:
        restore_pending_tool_request_for_approval_resume(
            checkpoint_state,
            now_provider=now_provider,
            paused_tool_step_id=paused_tool_step_id,
        )
        if set_aside:
            set_aside_resumed_tool_request(checkpoint_state)
        clear_current_approval_blocker(
            checkpoint_state,
            approval_session_id=parsed_request.approval_session_id,
            tool_request_id=parsed_request.tool_request_id,
        )
        clear_pending_approval_request(checkpoint_state)
        return checkpoint_state
    if approval_status == _APPROVAL_STATUS_APPROVED_ONCE:
        if getattr(approval_session, "claimed_at", None) is not None:
            clear_claimed_approval_resume_state(checkpoint_state)
            clear_current_approval_blocker(
                checkpoint_state,
                approval_session_id=parsed_request.approval_session_id,
                tool_request_id=parsed_request.tool_request_id,
            )
            clear_pending_approval_request(checkpoint_state)
            return checkpoint_state
        restore_pending_tool_request_for_approval_resume(
            checkpoint_state,
            now_provider=now_provider,
            paused_tool_step_id=paused_tool_step_id,
        )
        clear_current_approval_blocker(
            checkpoint_state,
            approval_session_id=parsed_request.approval_session_id,
            tool_request_id=parsed_request.tool_request_id,
        )
        clear_pending_approval_request(checkpoint_state)
        return checkpoint_state
    raise ResumeStateError(
        failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
        message=f"Unsupported approval session status: {approval_status!r}",
    )


def clear_pending_approval_request(checkpoint_state: ActionAgentState) -> None:
    checkpoint_state.pop("pending_approval_request", None)


def clear_claimed_approval_resume_state(
    checkpoint_state: ActionAgentState,
) -> None:
    ensure_context(checkpoint_state).pop("approval_resume_tool_request_id", None)
    checkpoint_state["next_action"] = None


def clear_current_approval_blocker(
    checkpoint_state: ActionAgentState,
    *,
    approval_session_id: str,
    tool_request_id: str,
) -> None:
    remaining = []
    for blocker in checkpoint_state.get("current_approval_blockers") or []:
        parsed = PendingApprovalRequestModel.model_validate(blocker)
        if (
            parsed.approval_session_id == approval_session_id
            and parsed.tool_request_id == tool_request_id
        ):
            continue
        remaining.append(parsed)
    if remaining:
        checkpoint_state["current_approval_blockers"] = remaining
    else:
        checkpoint_state.pop("current_approval_blockers", None)


def restore_pending_tool_request_for_approval_resume(
    checkpoint_state: ActionAgentState,
    *,
    now_provider: Callable[[], str],
    paused_tool_step_id: str | None = None,
) -> None:
    pending_request = checkpoint_state.get("pending_approval_request")
    if pending_request is None:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message="approval resume requires pending_approval_request in checkpoint",
        )
    try:
        parsed_request = PendingApprovalRequestModel.model_validate(pending_request)
    except ValidationError as exc:
        raise ResumeStateError(
            failure_code=ACTION_FAILURE_CODE_RESUME_APPROVAL_STATE_INVALID,
            message=f"Invalid pending approval request in checkpoint: {exc}",
        ) from exc

    restored_tool_call = build_tool_call(
        tool_id=parsed_request.tool_call.tool_id,
        args=dict(parsed_request.tool_call.args),
    )
    set_resumed_tool_request_id(
        state=checkpoint_state,
        tool_request_id=parsed_request.tool_request_id,
    )
    _require_supervisor_owner(parsed_request)
    checkpoint_state["next_action"] = build_next_action(
        tool=restored_tool_call,
        batch=_paused_tool_batch(checkpoint_state, approved_call=restored_tool_call),
        decided_at=str(checkpoint_state.get("updated_at") or now_provider()),
    )
    # The paused row already is this tool step; resuming settles it in place
    # instead of forking a second timeline entry for the same tool call.
    if paused_tool_step_id is not None:
        set_resumed_tool_step_id(
            state=checkpoint_state,
            step_id=paused_tool_step_id,
        )


def _paused_tool_batch(
    checkpoint_state: ActionAgentState,
    *,
    approved_call: ToolCallModel,
) -> PendingToolBatchModel | None:
    """pause した呼び出しと、その後ろに残っているバッチを返す。

    pause の checkpoint は「まだ実行していない呼び出し」を ``next_action`` に載せており、
    その先頭が pause した呼び出し自身である。ユーザーが承認・拒否したのはその 1 件なので、
    先頭が一致しない checkpoint の残バッチは信用せず、単発の再開に落とす。
    """

    next_action = checkpoint_state.get("next_action")
    if not isinstance(next_action, NextActionModel) or next_action.batch is None:
        return None
    if next_action.batch.calls[0].call != approved_call:
        return None
    return next_action.batch


__all__ = [
    "ApprovalSessionLoader",
    "clear_claimed_approval_resume_state",
    "clear_current_approval_blocker",
    "clear_pending_approval_request",
    "normalize_approval_resume_state",
    "restore_pending_tool_request_for_approval_resume",
    "select_approval_resume_target",
]

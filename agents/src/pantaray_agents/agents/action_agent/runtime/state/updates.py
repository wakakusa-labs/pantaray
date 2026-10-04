"""ActionAgent の state 更新を共通化するユーティリティ。"""

from __future__ import annotations

from pydantic import ValidationError

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import AgentError, JSONValue

from .types import ActionAgentState, ActionStatus, AgentErrorState


class StateErrorInvariantError(RuntimeError):
    """state.errors の不変条件違反。"""


type AgentErrorPayload = dict[str, JSONValue]


def touch_updated_at(
    state: ActionAgentState,
    *,
    updated_at: str | None = None,
) -> str:
    """state.updated_at を更新する。"""
    resolved = updated_at or now_utc_iso()
    state["updated_at"] = resolved
    return resolved


def set_status_with_updated_at(
    state: ActionAgentState,
    *,
    status: ActionStatus,
    updated_at: str | None = None,
) -> str:
    """state.status と state.updated_at を同時更新する。"""
    resolved = updated_at or now_utc_iso()
    state["status"] = status
    state["updated_at"] = resolved
    return resolved


def append_non_fatal_state_error(
    state: ActionAgentState,
    *,
    error_code: str,
    error_message: str,
    error_details: dict[str, JSONValue] | None = None,
    metadata: dict[str, JSONValue] | None = None,
    error_type: str = "internal_error",
    severity: str = "warning",
    updated_at: str | None = None,
) -> None:
    """非致命エラーを state.errors に追記し、updated_at を更新する。"""
    append_state_error_payload(
        state,
        error_payload={
            "error_type": error_type,
            "error_code": error_code,
            "error_message": error_message,
            "error_details": error_details,
            "severity": severity,
            "metadata": metadata,
        },
        updated_at=updated_at,
    )


def append_state_error(
    state: ActionAgentState,
    *,
    error: AgentError,
    prepend: bool = False,
    updated_at: str | None = None,
) -> None:
    """厳格検証済みの AgentError を state.errors へ追加する。"""
    raw_errors = state.get("errors")
    if raw_errors is None:
        errors: list[AgentErrorState] = []
        state["errors"] = errors
    elif isinstance(raw_errors, list):
        errors = raw_errors
    else:
        raise StateErrorInvariantError(
            "ActionAgent state invariant violated: errors must be a list."
        )

    entry: AgentErrorState = {
        "error_type": error.error_type,
        "error_code": error.error_code,
        "error_message": error.error_message,
        "error_details": error.error_details,
        "severity": error.severity,
        "metadata": error.metadata,
    }
    if prepend:
        errors.insert(0, entry)
    else:
        errors.append(entry)
    touch_updated_at(state, updated_at=updated_at)


def append_state_error_payload(
    state: ActionAgentState,
    *,
    error_payload: AgentErrorPayload,
    prepend: bool = False,
    updated_at: str | None = None,
) -> None:
    """error payload を AgentError で厳格検証して state.errors へ追加する。"""
    try:
        error = AgentError.model_validate(error_payload)
    except ValidationError as exc:
        raise StateErrorInvariantError(
            "ActionAgent state invariant violated: state.errors entry is invalid. "
            f"payload={error_payload!r}"
        ) from exc
    append_state_error(
        state,
        error=error,
        prepend=prepend,
        updated_at=updated_at,
    )


__all__ = [
    "append_state_error",
    "append_state_error_payload",
    "append_non_fatal_state_error",
    "StateErrorInvariantError",
    "set_status_with_updated_at",
    "touch_updated_at",
]

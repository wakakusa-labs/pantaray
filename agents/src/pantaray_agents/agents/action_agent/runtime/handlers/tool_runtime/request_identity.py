"""Approval-aware tool request identity helpers."""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.state.context import ensure_context
from pantaray_agents.utils.strict_numbers import is_strict_int

_APPROVAL_REQUEST_COUNTERS_KEY = "approval_request_counters"
_APPROVAL_RESUME_TOOL_REQUEST_ID_KEY = "approval_resume_tool_request_id"
_APPROVAL_RESUME_TOOL_STEP_ID_KEY = "approval_resume_tool_step_id"
_APPROVAL_SET_ASIDE_TOOL_REQUEST_ID_KEY = "approval_set_aside_tool_request_id"


def resolve_tool_request_id(*, state, step_id: str) -> str:
    """Return the canonical logical request id for the current tool execution."""

    resumed_tool_request_id = consume_resumed_tool_request_id(state)
    if resumed_tool_request_id is not None:
        return resumed_tool_request_id

    action_id = state.get("action_id")
    step_number = state.get("step")
    if not isinstance(action_id, str) or not action_id.strip():
        raise RuntimeError("tool request identity requires action_id")
    if not is_strict_int(step_number) or step_number < 1:
        raise RuntimeError("tool request identity requires state.step >= 1")

    context = ensure_context(state)
    raw_counters = context.get(_APPROVAL_REQUEST_COUNTERS_KEY)
    if raw_counters is None:
        counters: dict[str, int] = {}
        context[_APPROVAL_REQUEST_COUNTERS_KEY] = counters
    elif isinstance(raw_counters, dict) and all(
        isinstance(key, str) and is_strict_int(value) and value >= 0
        for key, value in raw_counters.items()
    ):
        counters = raw_counters
    else:
        raise RuntimeError(
            "ActionAgent context invariant violated: "
            "approval_request_counters must be a string->int map."
        )

    counter_key = str(step_number)
    next_index = counters.get(counter_key, 0) + 1
    counters[counter_key] = next_index
    return f"{action_id}:{step_id}:{next_index}"


def set_resumed_tool_request_id(*, state, tool_request_id: str) -> None:
    _set_resumed_identity(
        state,
        key=_APPROVAL_RESUME_TOOL_REQUEST_ID_KEY,
        value=tool_request_id,
        label="tool_request_id",
    )


def consume_resumed_tool_request_id(state) -> str | None:
    return _consume_resumed_identity(state, key=_APPROVAL_RESUME_TOOL_REQUEST_ID_KEY)


def set_resumed_tool_step_id(*, state, step_id: str) -> None:
    """Carry the paused tool step identity into its resumed execution."""

    _set_resumed_identity(
        state,
        key=_APPROVAL_RESUME_TOOL_STEP_ID_KEY,
        value=step_id,
        label="step_id",
    )


def consume_resumed_tool_step_id(state) -> str | None:
    return _consume_resumed_identity(state, key=_APPROVAL_RESUME_TOOL_STEP_ID_KEY)


def set_aside_resumed_tool_request(state) -> None:
    """Mark the restored call as answered by a new USER message instead.

    It must not run, and its request identity must not reach the next call, which
    would otherwise find this approval again.
    """

    tool_request_id = consume_resumed_tool_request_id(state)
    if tool_request_id is None:
        raise RuntimeError("Only a restored tool request can be set aside.")
    ensure_context(state)[_APPROVAL_SET_ASIDE_TOOL_REQUEST_ID_KEY] = tool_request_id


def consume_set_aside_tool_request(state) -> bool:
    """Whether the restored call's approval was answered by a new USER message."""

    return (
        _consume_resumed_identity(state, key=_APPROVAL_SET_ASIDE_TOOL_REQUEST_ID_KEY)
        is not None
    )


def _set_resumed_identity(state, *, key: str, value: str, label: str) -> None:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must be a non-empty string.")
    ensure_context(state)[key] = normalized


def _consume_resumed_identity(state, *, key: str) -> str | None:
    raw_value = ensure_context(state).pop(key, None)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise RuntimeError(
            f"ActionAgent context invariant violated: {key} must be a non-empty string."
        )
    return raw_value.strip()

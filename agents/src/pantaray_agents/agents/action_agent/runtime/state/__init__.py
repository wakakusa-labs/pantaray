"""ActionAgent runtime state package."""

from .coercion import coerce_int_with_invalid_reason
from .context import (
    copy_context,
    copy_state_with_context,
    ensure_context,
    get_context_view,
    require_context,
)
from .types import (
    ActionAgentContext,
    ActionAgentState,
    ActionAgentStateConfig,
    ActionPhase,
    ActionStatus,
    AgentErrorState,
    HistoryEntry,
    NextAction,
    PendingApprovalRequest,
    ToolCall,
    build_next_action,
    build_pending_approval_request,
    build_tool_call,
    create_initial_state,
)
from .updates import (
    StateErrorInvariantError,
    append_non_fatal_state_error,
    append_state_error,
    append_state_error_payload,
    set_status_with_updated_at,
    touch_updated_at,
)

__all__ = [
    "ActionAgentContext",
    "ActionAgentState",
    "ActionAgentStateConfig",
    "ActionPhase",
    "ActionStatus",
    "AgentErrorState",
    "HistoryEntry",
    "NextAction",
    "PendingApprovalRequest",
    "ToolCall",
    "StateErrorInvariantError",
    "append_non_fatal_state_error",
    "build_next_action",
    "build_tool_call",
    "append_state_error",
    "append_state_error_payload",
    "build_pending_approval_request",
    "coerce_int_with_invalid_reason",
    "copy_context",
    "copy_state_with_context",
    "create_initial_state",
    "ensure_context",
    "get_context_view",
    "require_context",
    "set_status_with_updated_at",
    "touch_updated_at",
]

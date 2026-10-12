"""ActionAgent runtime step helpers."""

from pantaray_agents.schema.agent.action_history import (
    GOAL_SCOPE_RE,
    SUPERVISOR_SCOPE_HANDLE,
)

from .counters import (
    DEFAULT_STEP_INCREMENT,
    CounterInvariantError,
    compute_step_counter_delta,
    get_llm_steps_taken,
    get_tool_steps_taken,
    increment_llm_steps_taken,
    increment_local_step_counter,
    increment_tool_steps_taken,
    require_local_step_counter_value,
    require_local_step_counters_map,
    require_scope_handle,
    require_step_counter_value,
    sync_steps_taken,
)
from .persistence import (
    ActionStepPersistenceError,
    persist_action_step,
)

__all__ = [
    "ActionStepPersistenceError",
    "compute_step_counter_delta",
    "CounterInvariantError",
    "DEFAULT_STEP_INCREMENT",
    "GOAL_SCOPE_RE",
    "SUPERVISOR_SCOPE_HANDLE",
    "get_llm_steps_taken",
    "get_tool_steps_taken",
    "increment_llm_steps_taken",
    "increment_local_step_counter",
    "increment_tool_steps_taken",
    "persist_action_step",
    "require_local_step_counter_value",
    "require_local_step_counters_map",
    "require_scope_handle",
    "require_step_counter_value",
    "sync_steps_taken",
]

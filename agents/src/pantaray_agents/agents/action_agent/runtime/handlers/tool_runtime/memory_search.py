"""memory_search ツール実行。"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    optional_object_arg as _optional_object_arg,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    optional_string_arg as _optional_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    require_int_arg as _require_int_arg,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    require_string_arg as _require_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_epoch,
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemorySearchResult,
)
from pantaray_agents.local_runtime.memory_catalog.search_policy import (
    parse_memory_search_focus,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MemorySearchRequest,
    execute_memory_search,
    memory_search_notes,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.repositories.repository import DBRow
from pantaray_agents.utils.local_time import describe_utc_timestamp

from ..tool_args import to_json_value
from .shared import (
    MemorySearchPayload,
    ToolRuntimeContext,
    UnprojectedToolExecutionResult,
)
from .validation import validate_tool_args


async def run_memory_search_tool(
    agent: object,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    *,
    tool_runtime_context: ToolRuntimeContext,
) -> UnprojectedToolExecutionResult:
    validate_tool_args(tool_def, args)

    user_id = state["user_id"]
    _ = agent, tool_runtime_context
    runtime_cfg = tool_def.runtime_config
    if runtime_cfg is None:
        raise RuntimeError("memory_search runtime config is required")
    default_limit = _required_runtime_limit(runtime_cfg.get("default_limit"))
    max_limit = _required_runtime_limit(runtime_cfg.get("max_limit"))

    query = _require_string_arg(args, "query")
    focus = _optional_string_arg(args, "focus") or "all"
    time_hint = _optional_object_arg(args, "time_hint")
    limit = _require_int_arg(args, "limit", default_limit)
    existing_epoch = _existing_epoch(state)
    run_id = existing_epoch.run_id if existing_epoch is not None else step_id

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    response = await execute_memory_search(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        request=MemorySearchRequest(
            user_id=user_id,
            run_id=run_id,
            query=query,
            focus=parse_memory_search_focus(focus),
            center_time=(
                str(time_hint["center"])
                if time_hint is not None and isinstance(time_hint.get("center"), str)
                else None
            ),
            radius_hours=(
                _time_hint_radius_hours(time_hint) if time_hint is not None else None
            ),
            limit=limit,
            current_epoch=existing_epoch,
        ),
    )
    records = [_serialize_memory_search_result(row) for row in response.results]
    epoch = response.epoch
    state["memory_context_epoch"] = serialize_memory_epoch(epoch)

    payload: MemorySearchPayload = {
        "results": records,
        "semantic_status": response.semantic_status,
        "semantic_error_code": response.semantic_error_code,
        "notes": memory_search_notes(response, limit=limit, max_limit=max_limit),
    }
    serialized_payload = to_json_value(payload)

    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=serialized_payload,
    )


def _serialize_memory_search_result(row: MemorySearchResult) -> DBRow:
    serialized = to_json_value(
        {**row, "observed_at": describe_utc_timestamp(row["observed_at"])}
    )
    if not isinstance(serialized, dict):
        raise RuntimeError("Memory Catalog search returned an invalid result")
    return serialized


def _time_hint_radius_hours(time_hint: dict[str, JSONValue]) -> int:
    radius_hours = time_hint.get("radius_hours")
    if isinstance(radius_hours, bool) or not isinstance(radius_hours, int):
        raise RuntimeError("memory_search time_hint.radius_hours is invalid")
    return radius_hours


def _required_runtime_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError("memory_search limit runtime config is invalid")
    return value


def _existing_epoch(state: ActionAgentState) -> MemoryContextEpoch | None:
    checkpoint = state.get("memory_context_epoch")
    return deserialize_memory_epoch(checkpoint) if checkpoint is not None else None

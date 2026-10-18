"""integration_search_tools as an Action tool.

Only whether the tools are offered is decided when the Action starts; the search
itself, in ``local_runtime.integrations.search``, resolves usability on every call.
"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_int_value,
    optional_string_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.integrations.search import (
    UPSTREAM_FAILED_MESSAGE,
    IntegrationGatewayError,
    search_integration_tools,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output

from .shared import UnprojectedToolExecutionResult


async def run_integration_search_tool(
    *, step_id: str, tool_def: ToolDefinition, args: ToolArgs, state
) -> UnprojectedToolExecutionResult:
    started_at = now_utc_iso()
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        output = await search_integration_tools(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=state["user_id"],
            query=require_string_arg(args, "query"),
            toolkit=optional_string_arg(args, "toolkit"),
            limit=optional_int_value(args.get("limit")),
            read_only=False,
        )
    except IntegrationGatewayError:
        return UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="error",
            started_at=started_at,
            completed_at=now_utc_iso(),
            output=build_runtime_tool_error_output(
                error_type="upstream_failed", message=UPSTREAM_FAILED_MESSAGE
            ),
        )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=started_at,
        completed_at=now_utc_iso(),
        output=output,
    )

"""integration_list_tools as an Action tool.

Only whether the tools are offered is decided when the Action starts; the list
itself, in ``local_runtime.integrations.search``, resolves usability on every call.
"""

from __future__ import annotations

from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    optional_string_arg,
    optional_string_list_arg,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.integrations.contracts import ToolClass
from pantaray_agents.local_runtime.integrations.search import (
    UPSTREAM_FAILED_MESSAGE,
    IntegrationGatewayError,
    IntegrationListArgumentError,
    list_integration_tools,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.tool_result import build_runtime_tool_error_output

from .shared import UnprojectedToolExecutionResult


async def run_integration_list_tool(
    *, step_id: str, tool_def: ToolDefinition, args: ToolArgs, state
) -> UnprojectedToolExecutionResult:
    started_at = now_utc_iso()
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        output = await list_integration_tools(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=state["user_id"],
            toolkit=require_string_arg(args, "toolkit"),
            # Validated against the input schema's enum.
            access=cast(ToolClass, require_string_arg(args, "access")),
            query=optional_string_arg(args, "query"),
            slugs=optional_string_list_arg(args, "slugs"),
            offer_remedies=True,
        )
    except IntegrationListArgumentError as exc:
        error_type, message = "invalid_arguments", str(exc)
    except IntegrationGatewayError:
        error_type, message = "upstream_failed", UPSTREAM_FAILED_MESSAGE
    else:
        return UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="success",
            started_at=started_at,
            completed_at=now_utc_iso(),
            output=output,
        )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="error",
        started_at=started_at,
        completed_at=now_utc_iso(),
        output=build_runtime_tool_error_output(error_type=error_type, message=message),
    )

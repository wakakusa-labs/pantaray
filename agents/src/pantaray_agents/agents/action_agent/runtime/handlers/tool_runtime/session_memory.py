"""write_session_memory tool execution.

Nothing is stored: the session memory is the latest successful call's content.
"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.session_memory_tool import (
    write_session_memory,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)


def run_write_session_memory_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError(
            f"{tool_def.tool_id} is only available to the Supervisor."
        )
    try:
        output = write_session_memory(require_string_arg(args, "content"))
    except ValueError as exc:
        message = str(exc)
        raise ToolValidationError(
            message, details={"path": ["args", "content"], "message": message}
        ) from exc
    timestamp = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=timestamp,
        completed_at=timestamp,
        output=output,
    )


__all__ = ["run_write_session_memory_tool"]

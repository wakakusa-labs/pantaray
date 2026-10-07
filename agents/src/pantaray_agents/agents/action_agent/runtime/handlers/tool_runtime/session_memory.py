"""write_session_memory tool execution.

Nothing is stored: the session memory is the latest successful call's content.
"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.tools import (
    SESSION_MEMORY_MAX_BYTES,
    ToolDefinition,
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
    size = len(require_string_arg(args, "content").encode("utf-8"))
    if size > SESSION_MEMORY_MAX_BYTES:
        message = (
            f"Session memory is {size:,} UTF-8 bytes, over the limit of "
            f"{SESSION_MEMORY_MAX_BYTES:,} bytes. Nothing was written; the "
            "previous session memory is still current. Shorten it and write again."
        )
        raise ToolValidationError(
            message, details={"path": ["args", "content"], "message": message}
        )
    timestamp = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=timestamp,
        completed_at=timestamp,
        output={
            "status": "written",
            "bytes": size,
            "limit_bytes": SESSION_MEMORY_MAX_BYTES,
        },
    )


__all__ = ["run_write_session_memory_tool"]

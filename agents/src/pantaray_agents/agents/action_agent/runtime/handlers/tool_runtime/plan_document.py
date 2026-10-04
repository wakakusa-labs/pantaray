"""Parent-only soft Action plan tool runtime."""

from __future__ import annotations

from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.agents.action_agent.tools.action_plan_tools import (
    READ_ACTION_PLAN_TOOL_ID,
    WRITE_ACTION_PLAN_TOOL_ID,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.action_plan_document import (
    ActionPlanTooLargeError,
    read_action_plan,
    write_action_plan,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)


async def run_action_plan_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError(
            f"{tool_def.tool_id} is only available to the Supervisor.",
            details={
                "path": ["tool_id"],
                "message": "Subagents cannot use the parent Action plan tools.",
                "metadata": {"actor": actor},
            },
        )

    db_path, _busy_timeout_ms = read_local_runtime_db_config()
    try:
        if tool_def.tool_id == READ_ACTION_PLAN_TOOL_ID:
            content = read_action_plan(
                db_path=db_path,
                user_id=state["user_id"],
                action_id=state["action_id"],
            )
            payload: dict[str, JSONValue] = {"status": "absent"}
            if content is not None:
                payload = {"status": "present", "content": content}
        elif tool_def.tool_id == WRITE_ACTION_PLAN_TOOL_ID:
            write_action_plan(
                db_path=db_path,
                user_id=state["user_id"],
                action_id=state["action_id"],
                content=cast(str, args["content"]),
            )
            payload = {"status": "written"}
        else:
            raise ValueError(f"Unsupported Action plan tool: {tool_def.tool_id}")
    except ActionPlanTooLargeError as exc:
        if tool_def.tool_id != WRITE_ACTION_PLAN_TOOL_ID:
            raise
        raise ToolValidationError(
            str(exc),
            details={
                "path": ["args", "content"],
                "message": str(exc),
            },
        ) from exc

    completed_at = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=completed_at,
        completed_at=completed_at,
        output=payload,
    )


__all__ = ["run_action_plan_tool"]

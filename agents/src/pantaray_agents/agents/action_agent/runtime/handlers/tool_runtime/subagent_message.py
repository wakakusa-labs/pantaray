from __future__ import annotations

from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.action_subagent_messages import (
    ActionSubagentMessageInputError,
    ActionSubagentMessageRequest,
    send_action_subagent_message,
)
from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    read_action_subagent_wait_snapshot,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import get_trace_context

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from .subagent_wait import _build_wait_request


async def run_send_message_to_subagent_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError("Only the Supervisor can send subagent messages.")
    trace = get_trace_context()
    process_id = trace.extra.get("process_id") if trace is not None else None
    if (
        trace is None
        or trace.user_id != state.get("user_id")
        or trace.action_id != state.get("action_id")
        or not isinstance(trace.local_job_id, str)
        or not trace.local_job_id.strip()
        or not isinstance(process_id, str)
        or not process_id.strip()
    ):
        raise RuntimeError("active Action job trace is required for subagent messages")

    accepted_at = now_utc_iso()
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        child_process_id = require_string_arg(args, "child_process_id")
        message_id = require_string_arg(args, "message_id")
        accepted = send_action_subagent_message(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ActionSubagentMessageRequest(
                user_id=state["user_id"],
                action_id=state["action_id"],
                parent_process_id=process_id,
                parent_job_id=trace.local_job_id,
                child_process_id=child_process_id,
                message_id=message_id,
                content=require_string_arg(args, "content"),
            ),
        )
    except ActionSubagentMessageInputError as exc:
        raise ToolValidationError(str(exc)) from exc
    if accepted:
        return UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id=tool_def.tool_id,
            status="success",
            started_at=accepted_at,
            completed_at=now_utc_iso(),
            output=True,
        )
    # A finished child, of this run or of one the user stopped, takes no message:
    # the parent gets its final result, as wait_subagents would give it. Collecting
    # it stays with wait_subagents.
    request = _build_wait_request(state=state, child_process_ids=(child_process_id,))
    snapshot = read_action_subagent_wait_snapshot(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, request=request
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=accepted_at,
        completed_at=now_utc_iso(),
        output=cast(JSONValue, snapshot.results[0]),
    )

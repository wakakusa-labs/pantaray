from __future__ import annotations

from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.action_subagent_cancel import (
    ActionSubagentCancelInputError,
    ActionSubagentCancelRequest,
    request_action_subagent_cancellation,
)
from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    ActionSubagentWaitInputError,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from .subagent_wait import _build_wait_request, _poll_wait


async def run_cancel_subagent_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError("Only the Supervisor can cancel subagents.")
    child_process_id = require_string_arg(args, "child_process_id")
    request = _build_wait_request(state=state, child_process_ids=(child_process_id,))
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    started_at = now_utc_iso()
    try:
        request_action_subagent_cancellation(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ActionSubagentCancelRequest(
                user_id=request.user_id,
                action_id=request.action_id,
                parent_process_id=request.parent_process_id,
                parent_job_id=request.parent_job_id,
                child_process_id=child_process_id,
            ),
        )
        snapshot = await _poll_wait(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=request,
        )
    except (ActionSubagentCancelInputError, ActionSubagentWaitInputError) as exc:
        raise ToolValidationError(str(exc)) from exc
    completed_at = now_utc_iso()
    receipt = (
        ActionSubagentCollectionReceipt(request=request, collected_at=completed_at)
        if snapshot.terminal_child_process_ids
        else None
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=started_at,
        completed_at=completed_at,
        output=cast(JSONValue, snapshot.results[0]),
        subagent_collection_receipt=receipt,
    )

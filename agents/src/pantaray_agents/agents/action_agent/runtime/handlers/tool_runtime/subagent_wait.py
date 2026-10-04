from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_list_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    ACTION_SUBAGENT_WAIT_MAX_SECONDS,
    ActionSubagentWaitInputError,
    ActionSubagentWaitSnapshot,
    read_action_subagent_wait_snapshot,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.action_subagent import (
    ActionSubagentCollectionReceipt,
    ActionSubagentWaitRequest,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import get_trace_context

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)

_WAIT_POLL_SECONDS = 0.1


def _build_wait_request(
    *, state: ActionAgentState, child_process_ids: tuple[str, ...]
) -> ActionSubagentWaitRequest:
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
        raise RuntimeError("active Action job trace is required for subagent wait")
    return ActionSubagentWaitRequest(
        user_id=state["user_id"],
        action_id=state["action_id"],
        parent_process_id=process_id,
        parent_job_id=trace.local_job_id,
        child_process_ids=child_process_ids,
    )


async def _poll_wait(
    *, db_path: Path, busy_timeout_ms: int, request: ActionSubagentWaitRequest
) -> ActionSubagentWaitSnapshot:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ACTION_SUBAGENT_WAIT_MAX_SECONDS
    while True:
        snapshot = read_action_subagent_wait_snapshot(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=request,
        )
        remaining = deadline - loop.time()
        if snapshot.all_terminal or remaining <= 0:
            return snapshot
        await asyncio.sleep(min(_WAIT_POLL_SECONDS, remaining))


async def run_wait_subagents_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError("Only the Supervisor can wait for subagents.")
    started_at = now_utc_iso()
    request = _build_wait_request(
        state=state,
        child_process_ids=tuple(require_string_list_arg(args, "child_process_ids")),
    )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        snapshot = await _poll_wait(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=request,
        )
    except ActionSubagentWaitInputError as exc:
        raise ToolValidationError(str(exc)) from exc
    completed_at = now_utc_iso()
    receipt = (
        ActionSubagentCollectionReceipt(
            request=replace(
                request, child_process_ids=snapshot.terminal_child_process_ids
            ),
            collected_at=completed_at,
        )
        if snapshot.terminal_child_process_ids
        else None
    )
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=started_at,
        completed_at=completed_at,
        output={"results": cast(JSONValue, list(snapshot.results))},
        subagent_collection_receipt=receipt,
    )

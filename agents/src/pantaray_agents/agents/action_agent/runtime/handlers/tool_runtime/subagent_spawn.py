from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.turn_input import (
    frozen_executing_head,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
    require_string_list_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.runtime.action_subagent_spawn import (
    ActionSubagentSpawnRequest,
    ActionSubagentSpawnRequestError,
    ExternalSpawnResourceClaim,
    WorkspaceSpawnResourceClaim,
    spawn_action_subagent,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
    ActionSubagentResourceClaimConflictError,
)
from pantaray_agents.local_runtime.tooling.action_subagent_resource_identity import (
    ActionSubagentResourceIdentityError,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.utils.trace_context import get_trace_context

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent


async def run_spawn_subagent_tool(
    agent: ActionAgent,
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
    origin: ActionToolCallOrigin | None,
) -> UnprojectedToolExecutionResult:
    if actor != "supervisor":
        raise ToolValidationError(
            "spawn_subagent is only available to the Supervisor.",
            details={"path": ["tool_id"], "message": "Subagents cannot spawn peers."},
        )
    if origin is None:
        raise ToolValidationError("spawn_subagent requires an adopted call origin.")
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
        raise RuntimeError("spawn_subagent requires the active Action job trace")

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    user_id = state["user_id"]
    action_id = state["action_id"]
    current_cwd = resolve_action_storage_paths(
        db_path=db_path,
        user_id=user_id,
        action_id=action_id,
    ).workspace
    try:
        result = spawn_action_subagent(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            request=ActionSubagentSpawnRequest(
                user_id=user_id,
                action_id=action_id,
                parent_process_id=process_id,
                parent_job_id=trace.local_job_id,
                root_execution_session_id=state["execution_session_id"],
                manifest_id=state["manifest_id"],
                origin=origin,
                model_selector=require_string_arg(args, "model"),
                action_context=frozen_executing_head(agent, state),
                task=require_string_arg(args, "task"),
                context_refs=tuple(require_string_list_arg(args, "context_refs")),
                resource_claims=_resource_claims(args, current_cwd=current_cwd),
                spawned_at=now_utc_iso(),
            ),
        )
    except (
        ActionSubagentSpawnRequestError,
        ActionSubagentResourceClaimConflictError,
        ActionSubagentResourceIdentityError,
    ) as exc:
        raise ToolValidationError(str(exc)) from exc
    completed_at = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=completed_at,
        completed_at=completed_at,
        output={"child_process_id": result.child_process_id},
    )


def _resource_claims(
    args: ToolArgs,
    *,
    current_cwd: Path,
) -> tuple[WorkspaceSpawnResourceClaim | ExternalSpawnResourceClaim, ...]:
    raw_claims = cast(list[dict[str, str]], args["resource_claims"])
    return tuple(
        WorkspaceSpawnResourceClaim(raw["path"], current_cwd)
        if raw["kind"] == "workspace_path"
        else ExternalSpawnResourceClaim(
            raw["root_identity"],
            raw["normalized_key"],
        )
        for raw in raw_claims
    )

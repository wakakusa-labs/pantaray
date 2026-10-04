"""remember tool execution."""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    require_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from .validation import validate_tool_args


async def run_remember_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    validate_tool_args(tool_def, args)
    if actor != "supervisor":
        raise ToolValidationError("remember is available only to the Supervisor.")
    action_id = state["action_id"]
    recorded_at = now_utc_iso()
    # The note stays one paragraph of literal text: a line starting with # would
    # split it into headings, and ref-like text must not become a semantic link.
    note = " ".join(require_string_arg(args, "note").splitlines())
    note = note.replace("[[ref:", r"[\[ref:")
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            register_inline_domain_memory(
                connection=connection,
                user_id=state["user_id"],
                source="memory_note",
                # Conversation deletion removes notes by this Action prefix.
                source_record_id=f"{action_id}:{step_id}",
                content=(
                    f"User request recorded {recorded_at} in Action {action_id}: "
                    f"{note}\n"
                ),
            )
    timestamp = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=timestamp,
        completed_at=timestamp,
        output={"status": "remembered"},
    )

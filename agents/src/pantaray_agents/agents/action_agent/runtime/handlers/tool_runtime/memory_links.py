from __future__ import annotations

import sqlite3

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    ToolArgs,
    to_json_value,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_draft,
    deserialize_memory_epoch,
    serialize_memory_draft,
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.draft import (
    link_memory,
    seal_link_command,
    unlink_memory,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    require_reference_source,
)
from pantaray_agents.local_runtime.memory_catalog.resolver import (
    follow_memory_reference,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import (
    ToolExecutionActor,
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from .validation import validate_tool_args


async def run_link_memory_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    validate_tool_args(tool_def, args)
    if actor != "supervisor":
        raise ToolValidationError(
            "link_memory is available only to the Supervisor draft."
        )
    draft_model = state.get("supervisor_memory_draft")
    epoch_model = state.get("memory_context_epoch")
    if draft_model is None or epoch_model is None:
        raise ToolValidationError(
            "link_memory requires a Supervisor draft and visible memory context."
        )
    draft = deserialize_memory_draft(draft_model)
    epoch = deserialize_memory_epoch(epoch_model)
    command = seal_link_command(
        epoch=epoch,
        draft=draft,
        tool_invocation_id=step_id,
        target_handle=_required_string(args, "target_handle"),
        source_path=_required_string(args, "source_path"),
        exact_text=_required_string(args, "exact_text"),
        occurrence=_required_int(args, "occurrence"),
        note=_required_string(args, "note"),
        expected_draft_revision=_required_string(args, "expected_draft_revision"),
    )
    updated = link_memory(draft=draft, command=command)
    state["supervisor_memory_draft"] = serialize_memory_draft(updated)
    state["supervisor_pending_final_answer"] = updated.documents[0].content
    return _result(
        step_id=step_id,
        tool_def=tool_def,
        payload={
            "status": "linked",
            "local_ref_id": command.local_ref_id,
            "draft_revision": updated.draft_revision,
        },
    )


async def run_unlink_memory_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
    actor: ToolExecutionActor,
) -> UnprojectedToolExecutionResult:
    validate_tool_args(tool_def, args)
    if actor != "supervisor":
        raise ToolValidationError(
            "unlink_memory is available only to the Supervisor draft."
        )
    draft_model = state.get("supervisor_memory_draft")
    if draft_model is None:
        raise ToolValidationError("unlink_memory requires a Supervisor draft.")
    updated = unlink_memory(
        draft=deserialize_memory_draft(draft_model),
        tool_invocation_id=step_id,
        local_ref_id=_required_string(args, "local_ref_id"),
        expected_draft_revision=_required_string(args, "expected_draft_revision"),
    )
    state["supervisor_memory_draft"] = serialize_memory_draft(updated)
    state["supervisor_pending_final_answer"] = updated.documents[0].content
    return _result(
        step_id=step_id,
        tool_def=tool_def,
        payload={"status": "unlinked", "draft_revision": updated.draft_revision},
    )


async def run_get_memory_reference_tool(
    *,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state: ActionAgentState,
) -> UnprojectedToolExecutionResult:
    validate_tool_args(tool_def, args)
    epoch_model = state.get("memory_context_epoch")
    if epoch_model is None:
        raise ToolValidationError(
            "get_memory_reference requires visible memory context."
        )
    epoch = deserialize_memory_epoch(epoch_model)
    source_item = require_reference_source(
        epoch=epoch,
        context_handle=_required_string(args, "source_handle"),
    )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        extended_epoch, resolved = follow_memory_reference(
            connection=connection,
            epoch=epoch,
            source_handle=source_item.item.context_handle,
            local_ref_id=_required_string(args, "local_ref_id"),
            enqueue_repair_on_failure=True,
        )
    state["memory_context_epoch"] = serialize_memory_epoch(extended_epoch)
    return _result(
        step_id=step_id,
        tool_def=tool_def,
        payload=to_json_value(resolved),
    )


def _required_string(args: ToolArgs, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise RuntimeError(f"{name} must be a string after validation")
    return value


def _required_int(args: ToolArgs, name: str) -> int:
    value = args.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{name} must be an integer after validation")
    return value


def _result(
    *, step_id: str, tool_def: ToolDefinition, payload: JSONValue
) -> UnprojectedToolExecutionResult:
    timestamp = now_utc_iso()
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=timestamp,
        completed_at=timestamp,
        output=payload,
    )

"""Supervisor final-answer draft tool runtime."""

from __future__ import annotations

import sqlite3
from typing import TypedDict, cast

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import ToolArgs
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolExecutionActor,
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_draft,
    serialize_memory_draft,
)
from pantaray_agents.local_runtime.memory_catalog.draft import (
    create_memory_draft,
    replace_text_draft,
)
from pantaray_agents.local_runtime.memory_catalog.models import MemoryDocument
from pantaray_agents.local_runtime.memory_catalog.repository import (
    ensure_preparing_node,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.agent.base import JSONValue

from .shared import UnprojectedToolExecutionResult

COMMON_DRAFT_NEXT_STEP = (
    "Review the stored draft. If it is ready to finalize, use the finalization "
    "tool for the current execution scope. If the answer text needs changes, "
    "call draft_final_answer again. If more work is needed, use the appropriate "
    "work tool."
)


class DraftFinalAnswerPayload(TypedDict):
    status: str
    next_step: str
    draft_revision: str


async def run_draft_final_answer_tool(
    _agent,
    step_id: str,
    tool_def: ToolDefinition,
    args: ToolArgs,
    state,
    *,
    actor: ToolExecutionActor = "supervisor",
) -> UnprojectedToolExecutionResult:
    answer = str(args["answer"]).strip()
    if actor != "supervisor":
        raise ToolValidationError(
            "draft_final_answer is only available to the Supervisor.",
            details={
                "path": ["tool_id"],
                "message": "Goal Workers submit propose_goal_completion instead.",
                "metadata": {"actor": actor},
            },
        )
    state["supervisor_pending_final_answer"] = answer
    existing = state.get("supervisor_memory_draft")
    if existing is None:
        db_path, busy_timeout_ms = read_local_runtime_db_config()
        with sqlite3.connect(db_path) as connection:
            configure_connection(connection, busy_timeout_ms)
            connection.row_factory = sqlite3.Row
            with connection:
                node = ensure_preparing_node(
                    connection=connection,
                    user_id=state["user_id"],
                    source="action",
                    source_record_id=state["action_id"],
                )
        draft = create_memory_draft(
            user_id=state["user_id"],
            owner_node_id=node.node_id,
            base_revision_id=node.current_revision_id,
            documents=(MemoryDocument("body.md", answer),),
        )
    else:
        draft = replace_text_draft(
            draft=deserialize_memory_draft(existing),
            body=answer,
        )
    state["supervisor_memory_draft"] = serialize_memory_draft(draft)

    result_payload: DraftFinalAnswerPayload = {
        "status": "draft_updated",
        "next_step": COMMON_DRAFT_NEXT_STEP,
        # link_memory and unlink_memory must name the revision they edit.
        "draft_revision": draft.draft_revision,
    }
    return UnprojectedToolExecutionResult(
        step_id=step_id,
        tool_id=tool_def.tool_id,
        status="success",
        started_at=now_utc_iso(),
        completed_at=now_utc_iso(),
        output=cast(JSONValue, result_payload),
    )


__all__ = [
    "COMMON_DRAFT_NEXT_STEP",
    "run_draft_final_answer_tool",
]

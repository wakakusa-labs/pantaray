"""Initial ノードの実装。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.agents_md import (
    load_pantaray_agents_md,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    has_persisted_user_request_step,
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    ExecutionContextModel,
    execution_context_from_state,
    require_execution_context_from_state,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentContext,
    ActionAgentState,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    ensure_context,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    get_context_view as _context_view,
)
from pantaray_agents.agents.action_agent.runtime.state.types import TargetContextState
from pantaray_agents.agents.action_agent.runtime.user_image_attachments import (
    build_user_image_attachments,
)
from pantaray_agents.local_runtime.integrations.search import (
    integration_tools_available,
)
from pantaray_agents.local_runtime.memory_catalog.memory_indexes import (
    load_memory_heads,
    render_memory_indexes,
)
from pantaray_agents.local_runtime.runtime.bootstrap import (
    read_local_runtime_artifact_root,
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.tooling.repository.manifests import (
    pin_action_memory_roots,
)
from pantaray_agents.schema.workspace_context import render_workspace_context_prompt
from pantaray_agents.tasks.action_user_message import render_action_user_request_text

from .assistant_message import project_persisted_assistant_messages
from .workspace_mount_catalog import load_required_local_workspace_manifest_catalog

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime


logger = logging.getLogger(__name__)


def _normalize_optional_text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


async def initialize_context(
    agent: ActionAgent,
    state: ActionAgentState,
    runtime: ActionGraphRuntime,
) -> ActionAgentState:
    """Init ノードで初期文脈を組み立てる。"""

    request = runtime.request
    current_user_message = request.user_message
    intervening_user_step = runtime.intervening_user_step
    context_user_message = (
        intervening_user_step.message
        if intervening_user_step is not None
        else current_user_message
    )
    suggestion_approval = context_user_message.suggestion_approval
    is_continuation = any(state.get("history_by_scope", {}).values())

    request_summary = (
        _normalize_optional_text(suggestion_approval.summary)
        if suggestion_approval is not None
        else None
    )
    target_context: TargetContextState = {
        "organization_name": (
            _normalize_optional_text(suggestion_approval.organization_name)
            if suggestion_approval is not None
            else None
        ),
        "project_name": (
            _normalize_optional_text(suggestion_approval.project_name)
            if suggestion_approval is not None
            else None
        ),
    }
    context = _context_view(state)
    previous_local_step_counters = dict(context.get("local_step_counters", {}))
    context.update(
        {
            "request_summary": request_summary,
            "target_context": target_context,
            "prompt_name": runtime.state_config["prompt_name"],
            "prompt_version": runtime.state_config["prompt_version"],
        }
    )
    context["additional_notes"] = []
    context["local_step_counters"] = (
        previous_local_step_counters if is_continuation else {}
    )
    execution_context = _require_execution_context(state)
    context["read_access_scope"] = execution_context.read_access_scope

    workspace_manifest_catalog = load_required_local_workspace_manifest_catalog(
        user_id=str(request.user_id),
        manifest_id=execution_context.manifest_id,
        execution_session_id=execution_context.execution_session_id,
        read_access_scope=execution_context.read_access_scope,
    )
    context["workspace_root_catalog"] = workspace_manifest_catalog.root_catalog
    context["workspace_context_prompt"] = render_workspace_context_prompt(
        workspace_manifest_catalog.workspace_context
    )
    context["agents_md_instructions"] = load_pantaray_agents_md()
    if "integration_tools_exposed" not in context:
        context["integration_tools_exposed"] = await integration_tools_available()

    # 親ランタイムは単一 ReAct のみ。mandatory planning phase を経由せず executing に
    # 直行する。
    state["context"] = context
    state["phase"] = "executing"
    if is_continuation:
        _reset_turn_plan(context)
    else:
        state["step"] = 1
        state["steps_taken"] = 0
        state["llm_steps_taken"] = 0
        state["tool_steps_taken"] = 0
        state["total_prompt_tokens"] = 0
        state["total_completion_tokens"] = 0
        state["tokens_used"] = 0
        state["history_by_scope"] = {"S": []}
    state["errors"] = []
    state["status"] = "processing"
    state["next_action"] = None
    state["chunks_sent"] = 0
    state["updated_at"] = datetime.now(UTC).isoformat()

    project_persisted_assistant_messages(
        state,
        request.preceding_assistant_messages,
        before_step_number=request.user_step_number,
    )
    if has_persisted_user_request_step(state, step_id=request.user_step_id):
        return state
    return project_persisted_user_request_step(
        state,
        step_id=request.user_step_id,
        step_number=request.user_step_number,
        local_step_number=request.user_step_local_step_number,
        short_step_id=request.user_step_short_id,
        request_text="\n\n".join(
            section
            for section in (
                render_action_user_request_text(current_user_message),
                request.stopped_run_note,
            )
            if section is not None
        ),
        occurred_at=request.user_step_created_at,
        history_phase="init",
        attachments=build_user_image_attachments(
            user_id=str(request.user_id),
            images=current_user_message.images,
        ),
    )


def ensure_memory_indexes(state: ActionAgentState, *, user_id: str) -> None:
    """Read the memory indexes the head shows from the revisions the Action reads.

    The revisions are the ones pinned in the Action's manifest as read-only
    folders, so an index's links resolve in the folders the Action is given.
    Runs before any route, so a run restored from a checkpoint gets them too,
    including a head it already froze: its later turns and the subagents it
    spawns render that frozen head. The indexes change only when a tree is
    pinned for the first time, such as on an Action created before its tree
    had a folder.
    """

    execution_context = execution_context_from_state(state)
    if execution_context is None:
        # A run whose resume failed carries no execution context and only
        # finalizes; every run that works gets one before the graph starts.
        return
    context = ensure_context(state)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    artifact_root = read_local_runtime_artifact_root()
    revisions = pin_action_memory_roots(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=artifact_root,
        user_id=user_id,
        manifest_id=execution_context.manifest_id,
    )
    indexes = render_memory_indexes(
        load_memory_heads(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            artifact_root=artifact_root,
            user_id=user_id,
            revisions=revisions,
        )
    )
    if context.get("memory_indexes") == indexes:
        return
    context["memory_indexes"] = indexes
    frozen = context.get("executing_head_fields")
    if frozen is not None:
        context["executing_head_fields"] = {**frozen, "memory_indexes": indexes}


def _reset_turn_plan(context: ActionAgentContext) -> None:
    context.pop("analysis_summary", None)
    context["tool_validation_error_streak"] = 0


def _require_execution_context(state: ActionAgentState) -> ExecutionContextModel:
    return require_execution_context_from_state(
        state,
        missing_message="Action execution context is required",
        invalid_message="Action execution context is incomplete",
    )


__all__ = ["ensure_memory_indexes", "initialize_context"]

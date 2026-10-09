"""Initial ノードの実装。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from pantaray_agents.agents.action_agent.runtime.agents_md import (
    load_pantaray_agents_md,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.user_request import (
    has_persisted_user_request_step,
    project_persisted_user_request_step,
)
from pantaray_agents.agents.action_agent.runtime.models.execution_context import (
    ExecutionContextModel,
    require_execution_context_from_state,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentContext,
    ActionAgentState,
)
from pantaray_agents.agents.action_agent.runtime.state.context import (
    get_context_view as _context_view,
)
from pantaray_agents.agents.action_agent.runtime.state.types import TargetContextState
from pantaray_agents.agents.action_agent.runtime.user_image_attachments import (
    build_user_image_attachments,
)
from pantaray_agents.agents.action_agent.support.repository_result import (
    ensure_repository_result,
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
    if not is_continuation:
        # Read once per Action: the head shows these briefs for the whole Action
        # and the model looks up anything else with its memory tools.
        context.update(await _load_profile_briefs(agent, runtime))
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
        request_text=render_action_user_request_text(current_user_message),
        occurred_at=request.user_step_created_at,
        history_phase="init",
        attachments=build_user_image_attachments(
            user_id=str(request.user_id),
            images=current_user_message.images,
        ),
    )


class _ProfileBriefs(TypedDict):
    insight_data: str
    structured_fact_data: str


async def _load_profile_briefs(
    agent: ActionAgent, runtime: ActionGraphRuntime
) -> _ProfileBriefs:
    """Read the insight and fact profile briefs the head shows."""

    initial_memory = ensure_repository_result(
        await agent.repository.get_initial_memory_context(runtime.request.user_id),
        "Failed to fetch atomic initial memory context.",
    )
    insight = initial_memory.insight
    return {
        "insight_data": runtime.services.rendering.build_insight_text(
            insight.insight_profile_brief if insight is not None else None
        ),
        "structured_fact_data": (
            initial_memory.facts.facts_profile_brief
            if initial_memory.facts is not None
            else ""
        ),
    }


def _reset_turn_plan(context: ActionAgentContext) -> None:
    context.pop("analysis_summary", None)
    context["tool_validation_error_streak"] = 0


def _require_execution_context(state: ActionAgentState) -> ExecutionContextModel:
    return require_execution_context_from_state(
        state,
        missing_message="Action execution context is required",
        invalid_message="Action execution context is incomplete",
    )


__all__ = ["initialize_context"]

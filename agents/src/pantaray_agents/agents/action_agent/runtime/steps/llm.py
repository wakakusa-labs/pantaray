"""LLM action step persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.runtime.steps.persistence import (
    ActionStepPersistenceError,
    persist_action_step,
)
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord, StepType
from pantaray_agents.schema.agent.action_assistant_message import ActionLlmTurnCommit
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent


class LLMStepPersistenceError(ActionStepPersistenceError):
    """LLM action step persistence failed."""


async def record_llm_step(
    agent: ActionAgent,
    state: ActionAgentState,
    *,
    step_id: str,
    action_id: str,
    step_number: int,
    step_name: str,
    llm_prompt_text: str | None,
    llm_response_text: str | None,
    thinking: str | None,
    status: str,
    started_at: str,
    completed_at: str,
    goal_handle: str,
    user_id: str,
    short_step_id: str,
    local_step_number: int,
    parent_step_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    retry_count: int = 0,
    checkpoint_state: ActionAgentState | None = None,
    llm_turn: ActionLlmTurnCommit | None = None,
    provider_turn: ActionProviderTurnRecord | None = None,
) -> None:
    """Persist one formal LLM step through the runtime persistence contract."""

    async def _save() -> RepositoryResult[DBRow]:
        return await agent.repository.save_action_step(
            step_id=step_id,
            action_id=action_id,
            step_number=step_number,
            step_name=step_name,
            step_type=StepType.LLM_OUTPUT,
            llm_prompt_text=llm_prompt_text,
            llm_response_text=llm_response_text,
            thinking=thinking,
            runtime_state_checkpoint=build_runtime_state_checkpoint(
                checkpoint_state or state
            ),
            runtime_state_checkpoint_version=RUNTIME_STATE_CHECKPOINT_VERSION,
            status=status,
            parent_step_id=parent_step_id,
            started_at=started_at,
            completed_at=completed_at,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            retry_count=retry_count,
            goal_handle=goal_handle,
            user_id=user_id,
            short_step_id=short_step_id,
            local_step_number=local_step_number,
            llm_turn=llm_turn,
            provider_turn=provider_turn,
        )

    try:
        await persist_action_step(
            save=_save,
            step_name=step_name,
            step_id=step_id,
        )
    except ActionStepPersistenceError as exc:
        raise LLMStepPersistenceError(str(exc)) from exc


__all__ = ["LLMStepPersistenceError", "record_llm_step"]

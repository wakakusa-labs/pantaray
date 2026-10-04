"""LLM ステップの記録（DB 永続化 + history 追加）を一元化する。"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.runtime.handlers.nodes import common
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.assistant_message import (
    build_assistant_history_entry,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    ActionPhase,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    CounterInvariantError,
)
from pantaray_agents.agents.action_agent.runtime.steps.llm import (
    record_llm_step as persist_llm_step,
)
from pantaray_agents.schema.agent.action import ActionProviderTurnRecord
from pantaray_agents.schema.agent.action_assistant_message import ActionLlmTurnCommit

from .builders import _build_llm_history_entry

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent

type ToolArgsPayload = dict[str, object]


async def record_llm_step(
    agent: ActionAgent,
    state: ActionAgentState,
    *,
    scope_handle: str,
    step_id: str,
    step_number: int,
    step_name: str,
    phase: ActionPhase,
    summary: str,
    llm_prompt_text: str | None,
    llm_response_text: str | None,
    status: str,
    started_at: str,
    completed_at: str,
    tool_id: str | None = None,
    thinking: str | None = None,
    result_line: str | None = None,
    args: ToolArgsPayload | None = None,
    short_step_id: str,
    local_step_number: int,
    parent_step_id: str | None = None,
    history_started_at: str | None = None,
    history_completed_at: str | None = None,
    infer_parent_from_scope_history: bool = False,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    checkpoint_state: ActionAgentState | None = None,
    llm_turn: ActionLlmTurnCommit | None = None,
    turn_context: str | None = None,
    world_state: dict[str, str] | None = None,
    provider_turn: ActionProviderTurnRecord | None = None,
) -> None:
    """LLM ステップを DB と state.history_by_scope に同時反映する。

    方針:
    - DB 保存を先に行い、保存成功時のみ history を追加する（history-only 進行を防ぐ）。
    - short_step_id/local_step_number は呼び出し側で決定し、DB と history で必ず一致させる。
    """
    if step_number < 1:
        raise CounterInvariantError(
            f"record_llm_step.step_number must be >= 1, got {step_number!r}"
        )
    resolved_parent_step_id = parent_step_id
    if resolved_parent_step_id is None and infer_parent_from_scope_history:
        scope_history = common.get_history_for_scope(state, scope_handle=scope_handle)
        if scope_history:
            raw_parent = scope_history[-1].get("step_id")
            if isinstance(raw_parent, str) and raw_parent.strip():
                resolved_parent_step_id = raw_parent.strip()

    history_entry = _build_llm_history_entry(
        step_id=step_id,
        step_number=step_number,
        phase=phase,
        summary=summary,
        tool_id=tool_id,
        started_at=history_started_at or started_at,
        completed_at=history_completed_at or completed_at,
        thinking=thinking,
        result_line=result_line,
        args=args,
        short_step_id=short_step_id,
        turn_context=turn_context,
        world_state=world_state,
    )
    history_entries = [
        build_assistant_history_entry(message, phase=phase)
        for message in (llm_turn.messages if llm_turn is not None else ())
    ]
    history_entries.append(history_entry)
    persisted_checkpoint_state = copy.deepcopy(checkpoint_state or state)
    for entry in history_entries:
        common.append_history_entry(
            persisted_checkpoint_state, scope_handle=scope_handle, entry=entry
        )

    await persist_llm_step(
        agent,
        state,
        action_id=state["action_id"],
        user_id=state["user_id"],
        step_id=step_id,
        step_number=step_number,
        step_name=step_name,
        llm_prompt_text=llm_prompt_text,
        llm_response_text=llm_response_text,
        thinking=thinking,
        status=status,
        parent_step_id=resolved_parent_step_id,
        started_at=started_at,
        completed_at=completed_at,
        goal_handle=scope_handle,
        short_step_id=short_step_id,
        local_step_number=local_step_number,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        checkpoint_state=persisted_checkpoint_state,
        llm_turn=llm_turn,
        # Stored on this row alone, never in the checkpoint: the checkpoint is
        # rewritten with the whole history on every step.
        provider_turn=provider_turn,
    )

    for entry in history_entries:
        common.append_history_entry(state, scope_handle=scope_handle, entry=entry)


__all__ = ["record_llm_step"]

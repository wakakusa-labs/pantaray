"""LLM ステップの組み立て処理。"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.state import (
    ActionPhase,
    HistoryEntry,
)
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue

type ToolArgsPayload = dict[str, JSONValue]


def _build_llm_history_entry(
    *,
    step_id: str,
    step_number: int,
    phase: ActionPhase,
    summary: str,
    tool_id: str | None,
    started_at: str,
    completed_at: str,
    thinking: str | None = None,
    result_line: str | None = None,
    args: ToolArgsPayload | None = None,
    short_step_id: str | None = None,
    turn_context: str | None = None,
    world_state: dict[str, str] | None = None,
) -> HistoryEntry:
    """LLM ステップの履歴エントリを組み立てる。"""
    entry: HistoryEntry = {
        "step_id": step_id,
        "step_number": step_number,
        "phase": phase,
        "step_type": StepType.LLM_OUTPUT,
        "summary": summary,
        "tool_id": tool_id,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    if thinking is not None:
        entry["thinking"] = thinking
    if result_line is not None:
        entry["result_line"] = result_line
    if args is not None:
        entry["args"] = args
    if short_step_id is not None:
        entry["short_step_id"] = short_step_id
    if turn_context is not None:
        entry["turn_context"] = turn_context
    if world_state is not None:
        entry["world_state"] = world_state
    return entry


__all__ = ["_build_llm_history_entry"]

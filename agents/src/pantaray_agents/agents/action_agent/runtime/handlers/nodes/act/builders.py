"""Action ノードの組み立て処理。"""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.state import (
    ActionPhase,
    HistoryEntry,
)
from pantaray_agents.agents.action_agent.runtime.tool_attachments import ToolAttachment
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue

type ToolArgsPayload = dict[str, JSONValue]


def _build_tool_history_entry(
    *,
    step_id: str,
    step_number: int,
    phase: ActionPhase,
    summary: str,
    tool_id: str,
    started_at: str,
    completed_at: str,
    result_line: str | None = None,
    args: ToolArgsPayload | None = None,
    output: JSONValue,
    attachments: list[ToolAttachment] | None = None,
    short_step_id: str | None = None,
    origin: ActionToolCallOrigin | None = None,
    agents_md: str | None = None,
) -> HistoryEntry:
    """ツール実行に関する履歴エントリを組み立てる。"""
    entry: HistoryEntry = {
        "step_id": step_id,
        "step_number": step_number,
        "phase": phase,
        "step_type": StepType.TOOL_EXECUTION,
        "summary": summary,
        "tool_id": tool_id,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    if result_line is not None:
        entry["result_line"] = result_line
    if args is not None:
        entry["args"] = args
    entry["output"] = output
    if attachments:
        entry["attachments"] = attachments
    if short_step_id is not None:
        entry["short_step_id"] = short_step_id
    if origin is not None:
        entry["call_id"] = origin.call_id
        entry["llm_step_id"] = origin.llm_step_id
    if agents_md is not None:
        entry["agents_md"] = agents_md
    return entry


__all__ = ["_build_tool_history_entry"]

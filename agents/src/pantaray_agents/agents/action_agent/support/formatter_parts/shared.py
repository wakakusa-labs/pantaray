"""Formatter shared types and utility helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue


@dataclass(frozen=True, slots=True)
class HistoryDisplayEntry:
    step_number: int
    phase: str
    step_type: StepType
    user_request_text: str | None
    tool_id: str | None
    result_line: str | None
    args_value: JSONValue | None
    args_present: bool
    summary: str | None
    output_present: bool
    output_value: JSONValue | None
    history_ref: str | None
    started_at: str
    completed_at: str
    attachment_refs: tuple[str, ...] = ()


def normalize_prompt_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def format_tool_summary_list(tool_registry: Mapping[str, ToolDefinition]) -> str:
    lines: list[str] = []
    for tool in tool_registry.values():
        description = normalize_prompt_text(tool.prompt_contract.description)
        lines.append(
            f"- Tool: {tool.name} | ID: {tool.tool_id} | Description: "
            f"{description or '(not set)'}"
        )
    return "\n".join(lines) if lines else "(no tools available)"


__all__ = [
    "HistoryDisplayEntry",
    "format_tool_summary_list",
    "normalize_prompt_text",
]

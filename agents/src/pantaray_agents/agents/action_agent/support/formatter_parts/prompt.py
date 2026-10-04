"""Prompt-oriented formatter mixin."""

from __future__ import annotations

from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
)
from pantaray_agents.agents.action_agent.runtime.state.context import get_context_view
from pantaray_agents.utils.local_time import describe_utc_timestamp
from pantaray_agents.utils.memory_source_policy import (
    MEMORY_SOURCE_GROUPS,
    MEMORY_SOURCE_ORDER,
    TOTAL_MEMORY_SOURCES,
)

from .shared import MemorySourceCoverageSlotMap


class PromptFormattingMixin:
    def format_memory_source_coverage(self, state: ActionAgentState) -> str:
        coverage = get_context_view(state)["memory_source_coverage"]
        evaluated_at = coverage["evaluated_at"].strip()
        if not evaluated_at:
            raise RuntimeError(
                "memory_source_coverage.evaluated_at must be a non-empty string"
            )
        slot_map: MemorySourceCoverageSlotMap = {}
        for slot in coverage["slots"]:
            source = slot["source"]
            if source not in MEMORY_SOURCE_ORDER:
                raise RuntimeError(
                    f"memory_source_coverage has unsupported source: {source}"
                )
            if source in slot_map:
                raise RuntimeError(
                    f"memory_source_coverage has duplicate source: {source}"
                )
            status = slot["status"]
            if status not in {"present", "missing", "unknown"}:
                raise RuntimeError(
                    f"memory_source_coverage has invalid status for source={source}: {status!r}"
                )
            slot_map[source] = (status, slot["latest_at"])
        if set(slot_map.keys()) != set(MEMORY_SOURCE_ORDER):
            missing_sources = sorted(set(MEMORY_SOURCE_ORDER) - set(slot_map.keys()))
            extra_sources = sorted(set(slot_map.keys()) - set(MEMORY_SOURCE_ORDER))
            raise RuntimeError(
                "memory_source_coverage slot key mismatch: "
                f"missing={missing_sources}, extra={extra_sources}"
            )
        present_count = 0
        unknown_count = 0
        lines: list[str] = [f"- evaluated_at: {describe_utc_timestamp(evaluated_at)}"]
        for source in MEMORY_SOURCE_ORDER:
            status, latest_at = slot_map[source]
            if status == "present":
                present_count += 1
            elif status == "unknown":
                unknown_count += 1
        lines.insert(1, f"- present_count: {present_count}/{TOTAL_MEMORY_SOURCES}")
        lines.insert(2, f"- unknown_count: {unknown_count}/{TOTAL_MEMORY_SOURCES}")
        for group_name, sources in MEMORY_SOURCE_GROUPS:
            lines.append(f"- {group_name}:")
            for source in sources:
                status, latest_at = slot_map[source]
                shown = describe_utc_timestamp(latest_at) if latest_at else "null"
                lines.append(f"  - {source}: {status} (latest_at: {shown})")
        return "\n".join(lines)

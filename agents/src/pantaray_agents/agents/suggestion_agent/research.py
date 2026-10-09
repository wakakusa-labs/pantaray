from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pantaray_agents.tools.contract import ReactToolDefinition


class SuggestionResearchTools(Protocol):
    def build_tool_definitions(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> tuple[ReactToolDefinition, ...]: ...


@dataclass(frozen=True, slots=True)
class FixedSuggestionResearchTools:
    definitions: tuple[ReactToolDefinition, ...]

    def build_tool_definitions(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> tuple[ReactToolDefinition, ...]:
        del user_id, run_id
        return self.definitions


__all__ = ["FixedSuggestionResearchTools", "SuggestionResearchTools"]

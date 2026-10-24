from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pantaray_agents.tools.contract import ReactToolDefinition


@dataclass(frozen=True, slots=True)
class SuggestionMakeTools:
    """The make stage's tools and the one folder they write into.

    ``folder`` exists and is canonical; the make stage removes it when it ends
    without files to show.
    """

    definitions: tuple[ReactToolDefinition, ...]
    folder: Path


class SuggestionResearchTools(Protocol):
    async def build_tool_definitions(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> tuple[ReactToolDefinition, ...]: ...

    async def build_make_tools(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> SuggestionMakeTools: ...


@dataclass(frozen=True, slots=True)
class FixedSuggestionResearchTools:
    definitions: tuple[ReactToolDefinition, ...]
    # None where no candidate is ever selected, as in mock mode.
    make_tools: SuggestionMakeTools | None = None

    async def build_tool_definitions(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> tuple[ReactToolDefinition, ...]:
        del user_id, run_id
        return self.definitions

    async def build_make_tools(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> SuggestionMakeTools:
        del user_id, run_id
        if self.make_tools is None:
            raise RuntimeError("These Suggestion tools have no make stage")
        self.make_tools.folder.mkdir(parents=True, exist_ok=True)
        return self.make_tools


__all__ = [
    "FixedSuggestionResearchTools",
    "SuggestionMakeTools",
    "SuggestionResearchTools",
]

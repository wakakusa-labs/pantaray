from __future__ import annotations

from pathlib import Path

from pantaray_agents.agents.suggestion_agent.make import MAKE_STAGE_TOOL_IDS
from pantaray_agents.agents.suggestion_agent.react import SUGGESTION_TOOL_IDS
from pantaray_agents.agents.suggestion_agent.research import (
    FixedSuggestionResearchTools,
    SuggestionMakeTools,
)
from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    tool_error_response,
)


async def _reject_mock_research(
    call: ReactToolCall,
    _step_number: int,
) -> ReactToolResult:
    return tool_error_response(
        tool_name=call.tool_name,
        error_code="MOCK_RESEARCH_UNAVAILABLE",
        message="Suggestion research is unavailable in mock mode.",
    )


def _mock_definition(tool_id: str) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=tool_id,
        description="Mock-mode Suggestion research boundary.",
        request_schema={"type": "object", "additionalProperties": True},
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"const": "success"}},
            }
        ),
        execute=_reject_mock_research,
    )


def build_mock_suggestion_research_tools(
    make_folder: Path | None = None,
) -> FixedSuggestionResearchTools:
    """Provide the complete fail-closed Suggestion tool contract in mock mode.

    With ``make_folder``, a selected candidate's make stage runs too, with every
    tool refusing; mock mode itself never selects one.
    """

    return FixedSuggestionResearchTools(
        definitions=tuple(_mock_definition(tool_id) for tool_id in SUGGESTION_TOOL_IDS),
        make_tools=None
        if make_folder is None
        else SuggestionMakeTools(
            tuple(_mock_definition(tool_id) for tool_id in MAKE_STAGE_TOOL_IDS),
            make_folder,
        ),
    )


__all__ = ["build_mock_suggestion_research_tools"]

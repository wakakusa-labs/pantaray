from __future__ import annotations

import pytest

from pantaray_agents.tools.contract import (
    ReactToolCall,
    ReactToolDefinition,
    ReactToolResult,
    react_tool_response_schema,
    resolve_react_tool_definitions,
)


def _tool(name: str) -> ReactToolDefinition:
    async def execute(call: ReactToolCall, _step_number: int) -> ReactToolResult:
        return ReactToolResult(
            tool_name=call.tool_name,
            status="success",
            output={"status": "success"},
        )

    return ReactToolDefinition(
        name=name,
        description=name,
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        response_schema=react_tool_response_schema(
            success_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"const": "success"}},
            }
        ),
        execute=execute,
    )


def test_resolve_react_tool_definitions_preserves_agent_order() -> None:
    selected = resolve_react_tool_definitions(
        definitions=(_tool("read"), _tool("grep"), _tool("web_search")),
        tool_ids=("web_search", "read"),
    )

    assert tuple(item.name for item in selected) == ("web_search", "read")


def test_resolve_react_tool_definitions_rejects_duplicate_definitions() -> None:
    with pytest.raises(ValueError, match="tool names must be unique"):
        resolve_react_tool_definitions(
            definitions=(_tool("read"), _tool("read")),
            tool_ids=("read",),
        )


def test_resolve_react_tool_definitions_rejects_duplicate_tool_ids() -> None:
    with pytest.raises(ValueError, match="tool ids must be unique"):
        resolve_react_tool_definitions(
            definitions=(_tool("read"),),
            tool_ids=("read", "read"),
        )


def test_resolve_react_tool_definitions_rejects_unknown_tool_id() -> None:
    with pytest.raises(ValueError, match="unknown React tool id: grep"):
        resolve_react_tool_definitions(
            definitions=(_tool("read"),),
            tool_ids=("grep",),
        )

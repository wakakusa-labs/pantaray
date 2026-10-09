from __future__ import annotations

from typing import TypedDict, cast

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import ReactToolResult

from .tool_result_store import (
    MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
    RunToolResultStore,
)

_OVERFLOW_ERROR_MESSAGE = (
    "Tool result exceeded the inline limit. Use tool_result_fetch to inspect it."
)


class _StoredReactToolResultEnvelope(TypedDict):
    output: JSONValue
    error_message: str | None


def project_tool_result(
    *,
    result: ReactToolResult,
    store: RunToolResultStore,
) -> ReactToolResult:
    """Project one oversized React result into the run-local result store."""

    serialized_output = serialize_json_tool_output(result.output)
    error_message_exceeds_limit = (
        result.error_message is not None
        and len(result.error_message) > MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )
    if (
        len(serialized_output) <= MEMORY_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        and not error_message_exceeds_limit
    ):
        return result

    envelope: _StoredReactToolResultEnvelope = {
        "output": result.output,
        "error_message": result.error_message,
    }
    metadata = store.store(
        serialize_json_tool_output(cast(dict[str, JSONValue], envelope))
    )
    return ReactToolResult(
        tool_name=result.tool_name,
        status=result.status,
        output=cast(dict[str, JSONValue], metadata),
        error_message=(
            _OVERFLOW_ERROR_MESSAGE
            if error_message_exceeds_limit
            else result.error_message
        ),
        final_step_recorded=result.final_step_recorded,
    )


__all__ = ["project_tool_result"]

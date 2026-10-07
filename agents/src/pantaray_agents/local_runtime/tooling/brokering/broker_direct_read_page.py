"""The read tool's envelope for one page of text, bounded to the inline budget."""

from __future__ import annotations

from typing import Literal

from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import BrokerPolicyError
from pantaray_agents.tools.files.text_lines import (
    READ_FILE_PAGE_LIMIT_RETRY_HINT,
    ReadLinesResult,
)

DEFAULT_READ_LIMIT = 2_000


def text_page_output(
    *,
    kind: Literal["file", "document"],
    path: str,
    offset: int,
    column: int,
    result: ReadLinesResult,
) -> dict[str, JSONValue]:
    """The read tool's envelope for one page of text."""

    return {
        "kind": kind,
        "path": path,
        "content": result.content,
        "offset": offset,
        "column": column,
        "end_line": result.end_line,
        "end_column": result.end_column,
        "total_lines": result.total_lines,
        "next_offset": result.next_offset,
        "next_column": result.next_column,
        "truncated": result.truncated,
        "truncation_reason": result.truncation_reason,
        "retry_hint": result.retry_hint,
    }


def bound_text_page(
    output: dict[str, JSONValue], *, content: str, offset: int, column: int
) -> dict[str, JSONValue]:
    """Keep text and its continuation inline through durable result projection."""
    if (
        len(serialize_json_tool_output(output))
        <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    ):
        return output

    def page(length: int) -> dict[str, JSONValue]:
        prefix = content[:length]
        line_count = prefix.count("\n")
        tail = prefix.rsplit("\n", 1)[-1]
        next_line = offset + line_count
        next_column = len(tail) + (1 if line_count else column)
        if prefix.endswith("\n"):
            end_line = next_line - 1
            last_line = prefix[:-1].rsplit("\n", 1)[-1]
            end_column = len(last_line) + (column - 1 if line_count == 1 else 0)
        else:
            end_line, end_column = next_line, next_column - 1
        return {
            **output,
            "content": prefix,
            "end_line": end_line,
            "end_column": end_column,
            "next_offset": next_line,
            "next_column": next_column,
            "truncated": True,
            "truncation_reason": "page_limit",
            "retry_hint": READ_FILE_PAGE_LIMIT_RETRY_HINT,
        }

    # JSON escaping and cursor metadata count toward the same storage limit.
    low, high = 0, len(content)
    while low < high:
        midpoint = (low + high + 1) // 2
        if (
            len(serialize_json_tool_output(page(midpoint)))
            <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
        ):
            low = midpoint
        else:
            high = midpoint - 1
    if low == 0:
        raise BrokerPolicyError("read metadata exceeds the inline output budget")
    return page(low)


__all__ = [
    "DEFAULT_READ_LIMIT",
    "bound_text_page",
    "text_page_output",
]

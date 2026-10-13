"""The read tool's envelope for one page of text, bounded to the inline budget."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from pantaray_agents.local_runtime.tooling.tool_result_storage import (
    ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.schema.tool_result import serialize_json_tool_output
from pantaray_agents.tools.contract import BrokerPolicyError

from .text_lines import (
    PageLine,
    ReadLinesResult,
    page_retry_hint,
    render_page_lines,
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
    output: dict[str, JSONValue], *, result: ReadLinesResult
) -> dict[str, JSONValue]:
    """Keep text and its continuation inline through durable result projection.

    The page ends after the last whole line that fits; only a first line that
    cannot fit alone is split, inside its own text, never inside a cut marker.
    """
    if _fits_inline(output):
        return output
    if not result.lines:
        raise BrokerPolicyError("read metadata exceeds the inline output budget")

    def page(
        lines: tuple[PageLine, ...], next_line: int, next_column: int
    ) -> dict[str, JSONValue]:
        last = lines[-1]
        return {
            **output,
            "content": render_page_lines(lines),
            "end_line": last.number,
            "end_column": last.end_column,
            "next_offset": next_line,
            "next_column": next_column,
            "truncated": True,
            "truncation_reason": "page_limit",
            "retry_hint": page_retry_hint(
                page_incomplete=True,
                line_count_incomplete=False,
                lines_cut=any(line.cut for line in lines),
            ),
        }

    def whole_lines(count: int) -> dict[str, JSONValue]:
        kept = result.lines[:count]
        return page(kept, kept[-1].number + 1, 1)

    def first_line_prefix(length: int) -> dict[str, JSONValue]:
        first = result.lines[0]
        prefix = replace(first, text=first.text[:length], newline="", cut=False)
        return page((prefix,), first.number, first.column + length)

    # JSON escaping and cursor metadata count toward the same storage limit.
    count = _largest_fitting(len(result.lines) - 1, whole_lines)
    if count:
        return whole_lines(count)
    length = _largest_fitting(len(result.lines[0].text), first_line_prefix)
    if length == 0:
        raise BrokerPolicyError("read metadata exceeds the inline output budget")
    return first_line_prefix(length)


def _fits_inline(output: JSONValue) -> bool:
    return (
        len(serialize_json_tool_output(output))
        <= ACTION_TOOL_RESULT_INLINE_CHARACTER_LIMIT
    )


def _largest_fitting(high: int, build: Callable[[int], dict[str, JSONValue]]) -> int:
    """The largest n in 0..high whose ``build(n)`` fits inline (0 when none)."""
    low = 0
    while low < high:
        midpoint = (low + high + 1) // 2
        if _fits_inline(build(midpoint)):
            low = midpoint
        else:
            high = midpoint - 1
    return low


__all__ = [
    "DEFAULT_READ_LIMIT",
    "bound_text_page",
    "text_page_output",
]

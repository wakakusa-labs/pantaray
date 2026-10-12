"""ActionAgent formatting facade."""

from __future__ import annotations

from .formatter_parts import (
    HistoryDisplayEntry,
    HistoryFormattingMixin,
    format_tool_summary_list,
    normalize_prompt_text,
)


class ActionAgentFormatter(
    HistoryFormattingMixin,
):
    """ActionAgent が扱う各種データの整形責務を束ねる facade。"""


__all__ = [
    "ActionAgentFormatter",
    "HistoryDisplayEntry",
    "format_tool_summary_list",
    "normalize_prompt_text",
]

from __future__ import annotations

from typing import Literal

MemoryCatalogSource = Literal[
    "activity_log",
    "source_records",
    "activity_summary",
    "suggestion",
    "action",
    "action_file_read",
    "memory_note",
    "agent_experience",
    "short_term_insight",
    "long_term_insight",
    "fact",
]

__all__ = ["MemoryCatalogSource"]

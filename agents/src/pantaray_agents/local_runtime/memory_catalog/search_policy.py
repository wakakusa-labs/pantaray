from __future__ import annotations

from typing import Literal, cast

from pantaray_agents.schema.memory_catalog import MemoryCatalogSource

MemorySearchSource = Literal[
    "activity_description",
    "source_records",
    "activity_summary",
    "suggestion",
    "action",
    "action_file_read",
    "memory_note",
    "agent_experience",
    "short_term_insight",
    "long_term_insight",
    "facts",
]
MemorySearchSelectionSource = Literal[
    "activity_description",
    "source_records",
    "activity_summary",
    "suggestion",
    "suggestions",
    "action",
    "actions",
    "action_file_read",
    "memory_note",
    "agent_experience",
    "short_term_insight",
    "long_term_insight",
    "facts",
]
MemorySearchFocus = Literal[
    "all",
    "user_context",
    "agent_work",
    "agent_experience",
    "activity",
    "stable_knowledge",
]

MEMORY_SEARCH_TO_CATALOG_SOURCE: dict[
    MemorySearchSelectionSource, MemoryCatalogSource
] = {
    "activity_description": "activity_log",
    "source_records": "source_records",
    "activity_summary": "activity_summary",
    "suggestion": "suggestion",
    "suggestions": "suggestion",
    "action": "action",
    "actions": "action",
    "action_file_read": "action_file_read",
    "memory_note": "memory_note",
    "agent_experience": "agent_experience",
    "short_term_insight": "short_term_insight",
    "long_term_insight": "long_term_insight",
    "facts": "fact",
}
MEMORY_CATALOG_TO_SEARCH_SOURCE: dict[MemoryCatalogSource, MemorySearchSource] = {
    "activity_log": "activity_description",
    "source_records": "source_records",
    "activity_summary": "activity_summary",
    "suggestion": "suggestion",
    "action": "action",
    "action_file_read": "action_file_read",
    "memory_note": "memory_note",
    "agent_experience": "agent_experience",
    "short_term_insight": "short_term_insight",
    "long_term_insight": "long_term_insight",
    "fact": "facts",
}
MEMORY_SEARCH_FOCUS_SOURCES: dict[
    MemorySearchFocus, tuple[MemorySearchSelectionSource, ...]
] = {
    "all": (
        "long_term_insight",
        "facts",
        "memory_note",
        "short_term_insight",
        "suggestions",
        "actions",
        "action_file_read",
        "agent_experience",
        "activity_summary",
        "activity_description",
        "source_records",
    ),
    "user_context": ("long_term_insight", "facts", "memory_note", "suggestions"),
    "agent_work": ("actions", "action_file_read", "agent_experience"),
    "agent_experience": ("agent_experience",),
    "activity": (
        "short_term_insight",
        "activity_summary",
        "activity_description",
        "source_records",
    ),
    "stable_knowledge": ("long_term_insight", "facts"),
}
MEMORY_SEARCH_FOCUS_VALUES = tuple(MEMORY_SEARCH_FOCUS_SOURCES)


def parse_memory_search_focus(value: str) -> MemorySearchFocus:
    if value not in MEMORY_SEARCH_FOCUS_SOURCES:
        raise ValueError("memory search focus is invalid")
    return cast(MemorySearchFocus, value)


__all__ = [
    "MEMORY_CATALOG_TO_SEARCH_SOURCE",
    "MEMORY_SEARCH_FOCUS_SOURCES",
    "MEMORY_SEARCH_FOCUS_VALUES",
    "MEMORY_SEARCH_TO_CATALOG_SOURCE",
    "MemoryCatalogSource",
    "MemorySearchFocus",
    "MemorySearchSelectionSource",
    "MemorySearchSource",
    "parse_memory_search_focus",
]

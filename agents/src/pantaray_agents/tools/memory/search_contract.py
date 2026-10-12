"""The one model-facing contract of memory_search, shared by every agent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pantaray_agents.local_runtime.memory_catalog.search_policy import (
    MemorySearchFocus,
)
from pantaray_agents.schema.agent.base import JSONValue

# Action renders these as its Purpose / When / Avoid sections; every other
# agent reads them as one paragraph.
MEMORY_SEARCH_PURPOSE = (
    "Search memory: what is known about the user, their recent activity, and past "
    "agent work. Each result's matched_by says whether keywords, query, or both "
    "found it."
)
MEMORY_SEARCH_WHEN = (
    "Search before answering or acting when what is known about the user, a "
    "project, recent activity, or past agent work may change the answer or the "
    "work. keywords finds records holding the words you give; query finds records"
    " by meaning, so it also finds records worded differently. Give at least one;"
    " most searches should give both."
)
MEMORY_SEARCH_AVOID = (
    "Long-term insights and facts can be stale; when it matters, check them "
    "against recent activity. observed_at is the latest time in the result's "
    "record, not when a quote was observed; read the quote's Observed: line for "
    "that. notes in the result name any keyword that was not matched and say when"
    " the result list was full."
)
MEMORY_SEARCH_DESCRIPTION = " ".join(
    (MEMORY_SEARCH_PURPOSE, MEMORY_SEARCH_WHEN, MEMORY_SEARCH_AVOID)
)
KEYWORDS_DESCRIPTION = (
    "Names, IDs, file paths, error text, and other distinctive words, each as it "
    "would be written in the record. An item equal to a record ID or a memory file "
    "path returns that record first. Words are matched from three characters; "
    "below that, single characters are not matched, and two-letter ASCII words "
    "only whole and when written in capitals (PR, UI) or with a digit (#7, v2)."
)
QUERY_DESCRIPTION = (
    "A sentence saying what you want to know, not a list of words. To find lessons "
    "from past work, describe the task at hand, e.g. 'updating a Python dependency "
    "for a security advisory in the desktop app'."
)
_FOCUS_DESCRIPTIONS: dict[MemorySearchFocus, str] = {
    "all": "everything below",
    "user_context": (
        "what is known about the user: long-term insights, facts, notes, past "
        "suggestions"
    ),
    "agent_work": "past Actions, the files they read, and agent experience",
    "agent_experience": (
        "lessons and procedures from past work, each written for the situation it "
        "applies to"
    ),
    "activity": (
        "what the user did recently: activity logs and summaries, source records, "
        "short-term insights"
    ),
    "stable_knowledge": "long-term insights and facts",
}
MEMORY_SEARCH_INPUT_ERROR = "Give keywords, query, or both."


@dataclass(frozen=True, slots=True)
class MemorySearchInput:
    keywords: tuple[str, ...]
    query: str | None


def keywords_schema() -> dict[str, JSONValue]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "minItems": 1,
    }


def query_schema() -> dict[str, JSONValue]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": r"\S",
    }


def focus_description(
    allowed: Sequence[MemorySearchFocus], default: MemorySearchFocus | None
) -> str:
    choices = " ".join(f"{focus}: {_FOCUS_DESCRIPTIONS[focus]}." for focus in allowed)
    default_text = f" Default {default}." if default is not None else ""
    return f"Which memory to search.{default_text} {choices}"


def parse_memory_search_input(args: Mapping[str, JSONValue]) -> MemorySearchInput:
    """Raises ValueError when neither input is usable or one has the wrong type."""

    raw_keywords = args.get("keywords")
    raw_query = args.get("query")
    if raw_keywords is not None and (
        not isinstance(raw_keywords, list)
        or not all(isinstance(word, str) for word in raw_keywords)
    ):
        raise ValueError("keywords must be a list of strings.")
    if raw_query is not None and not isinstance(raw_query, str):
        raise ValueError("query must be a string.")
    # The type check above makes isinstance a no-op here; it narrows for mypy.
    words = (word.strip() for word in raw_keywords or () if isinstance(word, str))
    keywords = tuple(dict.fromkeys(word for word in words if word))
    query = raw_query.strip() if raw_query is not None else ""
    if not keywords and not query:
        raise ValueError(MEMORY_SEARCH_INPUT_ERROR)
    return MemorySearchInput(keywords=keywords, query=query or None)


__all__ = [
    "MEMORY_SEARCH_AVOID",
    "MEMORY_SEARCH_DESCRIPTION",
    "KEYWORDS_DESCRIPTION",
    "MEMORY_SEARCH_INPUT_ERROR",
    "MEMORY_SEARCH_PURPOSE",
    "MEMORY_SEARCH_WHEN",
    "QUERY_DESCRIPTION",
    "MemorySearchInput",
    "focus_description",
    "keywords_schema",
    "parse_memory_search_input",
    "query_schema",
]

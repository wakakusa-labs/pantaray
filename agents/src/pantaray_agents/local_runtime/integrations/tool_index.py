"""In-memory index of the connected toolkits' tools, searched without the gateway.

The backend fetches each connected toolkit's tools once (at startup and on
connection) and replaces that toolkit's entry wholesale; search never calls the
gateway, so it costs no external request.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from threading import Lock
from typing import Final

from .contracts import IntegrationTool, ToolkitSlug

SEARCH_LIMIT_DEFAULT: Final = 5
SEARCH_LIMIT_MAX: Final = 8

# Letters and digits of any script; '_' separates words in slugs such as GMAIL_SEND_EMAIL.
_WORD_RE: Final = re.compile(r"[^\W_]+")
# A word in the slug or name says what the tool does; one only in the description
# may just mention it ("returns the email's sender").
_TITLE_WEIGHT: Final = 2.0
_DESCRIPTION_WEIGHT: Final = 1.0


def _terms(text: str) -> set[str]:
    # Fold a plain English plural so "emails" meets EMAIL; "address" keeps its "ss".
    return {
        word[:-1]
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss")
        else word
        for word in _WORD_RE.findall(text.casefold())
    }


class _Entry:
    __slots__ = ("description", "title", "tool")

    def __init__(self, tool: IntegrationTool) -> None:
        self.tool = tool
        self.title = _terms(tool.slug) | _terms(tool.name)
        self.description = _terms(tool.description)


class ToolIndex:
    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[ToolkitSlug, tuple[_Entry, ...]] = {}

    def replace(self, toolkit: ToolkitSlug, tools: Iterable[IntegrationTool]) -> None:
        """Make ``tools`` the toolkit's whole tool list; an empty list removes it."""
        entries = tuple(_Entry(tool) for tool in tools)
        with self._lock:
            if entries:
                self._entries[toolkit] = entries
            else:
                self._entries.pop(toolkit, None)

    def get(self, slug: str) -> IntegrationTool | None:
        for entry in self._snapshot(None):
            if entry.tool.slug == slug:
                return entry.tool
        return None

    def tools(self, toolkit: ToolkitSlug) -> list[IntegrationTool]:
        return [entry.tool for entry in self._snapshot(toolkit)]

    def search(
        self,
        query: str,
        *,
        usable: Callable[[IntegrationTool], bool],
        toolkit: ToolkitSlug | None = None,
        limit: int = SEARCH_LIMIT_DEFAULT,
    ) -> list[IntegrationTool]:
        """Rank the usable tools by the query's words found in slug, name and description.

        ``usable`` filters before ranking, so a tool the caller may not use never
        takes a place in the top ``limit``.

        Design limit: word overlap only. Switch to the bundled embedding model
        when Action replays show searches missing the tool the task needed.
        """
        candidates = [entry for entry in self._snapshot(toolkit) if usable(entry.tool)]
        query_terms = _terms(query)
        if not candidates or not query_terms:
            return []
        weights = {term: _weight(term, candidates) for term in query_terms}
        scored = [
            (score, entry.tool)
            for entry in candidates
            if (score := _score(entry, weights)) > 0
        ]
        scored.sort(key=lambda item: (-item[0], item[1].slug))
        return [tool for _, tool in scored[:limit]]

    def _snapshot(self, toolkit: ToolkitSlug | None) -> Sequence[_Entry]:
        with self._lock:
            if toolkit is not None:
                return self._entries.get(toolkit, ())
            return [entry for entries in self._entries.values() for entry in entries]


def _weight(term: str, candidates: Sequence[_Entry]) -> float:
    # A word every candidate holds (the toolkit's name) tells them apart least.
    holders = sum(term in e.title or term in e.description for e in candidates)
    return math.log(1 + len(candidates) / holders) if holders else 0.0


def _score(entry: _Entry, weights: dict[str, float]) -> float:
    score = 0.0
    for term, weight in weights.items():
        if term in entry.title:
            score += weight * _TITLE_WEIGHT
        elif term in entry.description:
            score += weight * _DESCRIPTION_WEIGHT
    return score

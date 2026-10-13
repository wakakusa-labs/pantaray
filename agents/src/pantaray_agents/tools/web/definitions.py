"""web_search and web_extract as the model sees them, once for every agent.

The Action builds its catalog entries from these words and schemas, and
Suggestion and the chat build theirs in ``session``; all three run them through
``execute``. A long result is not paged here: the host stores it like any other
tool result too large to keep inline, and the model reads the rest from there.
"""

from __future__ import annotations

from typing import Final

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.profiles import (
    WEB_EXCERPTS_PER_PAGE,
    WEB_SEARCH_COUNTRIES,
    WEB_SEARCH_MAX_QUERIES,
    WEB_SEARCH_RESULT_LIMIT,
    WEB_SEARCH_TOPICS,
)

WEB_SEARCH_TOOL_NAME: Final = "web_search"
WEB_EXTRACT_TOOL_NAME: Final = "web_extract"
WEB_EXTRACT_MAX_URLS: Final = 5
WEB_URL_MAX_CHARS: Final = 2_048

# What, when and what to avoid: the Action shows them as its tool guide, and
# Suggestion and the chat show the same three as one description.
WEB_SEARCH_WHAT: Final = (
    f"Search the web for up to {WEB_SEARCH_MAX_QUERIES} queries at once. Each "
    f"query is its own search and returns up to {WEB_SEARCH_RESULT_LIMIT} results, "
    "each with an excerpt of its page, under that query; a URL another query "
    "already found is listed only under the first. Use web_extract to read a "
    "whole page."
)
WEB_SEARCH_WHEN: Final = (
    "Use when the answer depends on public facts that memory and the current "
    "context do not have: products, documentation, news, prices, or people and "
    "organisations outside the user's own records."
)
WEB_SEARCH_PITFALLS: Final = (
    "Give each product, source or angle a query of its own rather than one query "
    "that covers them all, and send them together. Set topic only when it "
    "changes what should "
    "be found (news for recent reporting, finance for markets and companies), "
    "and country only with the general topic when regional results matter."
)

WEB_EXTRACT_WHAT: Final = (
    f"Read up to {WEB_EXTRACT_MAX_URLS} known web pages and return each page's "
    "main text; a page that could not be read is listed with the reason. With "
    f"query, each page returns only its top {WEB_EXCERPTS_PER_PAGE} excerpts (up "
    "to 500 characters each) relevant to it. When meta.upstream_provider is "
    '"local" (no Pantaray sign-in or Tavily key), the pages were fetched from '
    "this computer: query is ignored and each page comes back whole, and a page "
    "that builds its content with JavaScript may come back without text."
)
WEB_EXTRACT_WHEN: Final = (
    "Use after web_search, or when the user or a source gave concrete page URLs "
    "whose content is needed."
)
WEB_EXTRACT_PITFALLS: Final = (
    "Pass the page itself, not a homepage unless the homepage is the target. Set "
    "query only when excerpts are enough; omit it to read the whole page."
)

WEB_SEARCH_FIELDS: Final[dict[str, JSONValue]] = {
    "queries": {
        "type": "array",
        "minItems": 1,
        "maxItems": WEB_SEARCH_MAX_QUERIES,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "description": (
            f"1 to {WEB_SEARCH_MAX_QUERIES} search queries, each focused on one "
            "subject; they run side by side."
        ),
    },
    "topic": {
        "type": "string",
        "enum": list(WEB_SEARCH_TOPICS),
        "description": (
            "Optional. general for broad web search, news for recent reporting, "
            "finance for markets and companies."
        ),
    },
    "country": {
        "type": "string",
        "enum": list(WEB_SEARCH_COUNTRIES),
        "description": (
            "Optional. Prefer results from this country; only with the general topic."
        ),
    },
}
WEB_SEARCH_REQUIRED: Final = ("queries",)

WEB_EXTRACT_FIELDS: Final[dict[str, JSONValue]] = {
    "urls": {
        "type": "array",
        "minItems": 1,
        "maxItems": WEB_EXTRACT_MAX_URLS,
        "uniqueItems": True,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": WEB_URL_MAX_CHARS,
            "pattern": r"^https?://\S+$",
        },
        "description": (
            f"Page URLs to read (at most {WEB_EXTRACT_MAX_URLS}). Addresses on this "
            "computer or a private network are refused."
        ),
    },
    "query": {
        "type": "string",
        "minLength": 1,
        "pattern": r"\S",
        "description": (
            f"Optional. Return only each page's top {WEB_EXCERPTS_PER_PAGE} excerpts "
            "relevant to this text instead of the whole page."
        ),
    },
}
WEB_EXTRACT_REQUIRED: Final = ("urls",)

_META: Final[dict[str, JSONValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["request_id", "upstream_provider", "profile_id", "outcome"],
    "properties": {
        "request_id": {"type": "string"},
        "upstream_provider": {"type": "string"},
        "profile_id": {"type": "string"},
        "outcome": {
            "type": "string",
            "enum": ["complete", "no_results", "partial_results"],
        },
        "upstream_request_id": {"type": "string"},
        "suggested_action": {"type": "string"},
    },
}
_NULLABLE_STRING: Final[dict[str, JSONValue]] = {"type": ["string", "null"]}

_SEARCH_FOUND: Final[dict[str, JSONValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query", "results", "meta"],
    "properties": {
        "query": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url", "content", "score"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "content": {"type": "string"},
                    "score": {"type": "number"},
                },
            },
        },
        "meta": _META,
    },
}
_SEARCH_FAILED: Final[dict[str, JSONValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query", "error"],
    "properties": {"query": {"type": "string"}, "error": {"type": "string"}},
}
WEB_SEARCH_RESULT_SCHEMA: Final[dict[str, JSONValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "searches"],
    "properties": {
        "status": {"const": "success"},
        "searches": {
            "type": "array",
            "items": {"oneOf": [_SEARCH_FOUND, _SEARCH_FAILED]},
        },
    },
}

WEB_EXTRACT_RESULT_SCHEMA: Final[dict[str, JSONValue]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "results", "failed_results", "retry_hint", "meta"],
    "properties": {
        "status": {"const": "success"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "raw_content"],
                "properties": {
                    "url": {"type": "string"},
                    "raw_content": {"type": "string"},
                },
            },
        },
        "failed_results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "error"],
                "properties": {
                    "url": {"type": "string"},
                    "error": {"type": "string"},
                },
            },
        },
        "retry_hint": _NULLABLE_STRING,
        "meta": _META,
    },
}


def request_schema(
    fields: dict[str, JSONValue], required: tuple[str, ...]
) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": fields,
        "required": list(required),
    }


__all__ = [
    "WEB_EXTRACT_FIELDS",
    "WEB_EXTRACT_MAX_URLS",
    "WEB_EXTRACT_PITFALLS",
    "WEB_EXTRACT_REQUIRED",
    "WEB_EXTRACT_RESULT_SCHEMA",
    "WEB_EXTRACT_TOOL_NAME",
    "WEB_EXTRACT_WHAT",
    "WEB_EXTRACT_WHEN",
    "WEB_SEARCH_FIELDS",
    "WEB_SEARCH_PITFALLS",
    "WEB_SEARCH_REQUIRED",
    "WEB_SEARCH_RESULT_SCHEMA",
    "WEB_SEARCH_TOOL_NAME",
    "WEB_SEARCH_WHAT",
    "WEB_SEARCH_WHEN",
    "request_schema",
]

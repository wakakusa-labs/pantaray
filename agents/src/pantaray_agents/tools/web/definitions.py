from __future__ import annotations

from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import (
    ReactToolDefinition,
    ReactToolExecutor,
    ToolConcurrency,
    react_tool_response_schema,
)
from pantaray_llm.profiles import WEB_EXCERPTS_PER_PAGE, WEB_SEARCH_RESULT_LIMIT

WEB_EXTRACT_MAX_URLS = 3
WEB_URL_MAX_CHARS = 2_048
WEB_SEARCH_MAX_RESULTS = 8
WEB_SEARCH_RESULT_CONTENT_MAX_CHARS = 600
WEB_EXTRACT_PAGE_MAX_CHARS = 1_400

_NULLABLE_STRING: dict[str, JSONValue] = {"type": ["string", "null"]}
_PAGE_PROPERTIES: dict[str, JSONValue] = {
    "next_offset": {"type": ["integer", "null"], "minimum": 1},
    "truncated": {"type": "boolean"},
    "truncation_reason": _NULLABLE_STRING,
}


def _success_schema(
    *, properties: dict[str, JSONValue], required: tuple[str, ...]
) -> dict[str, JSONValue]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", *required],
        "properties": {"status": {"const": "success"}, **properties},
    }


def _definition(
    *,
    name: str,
    description: str,
    properties: dict[str, JSONValue],
    required: tuple[str, ...],
    success_schema: dict[str, JSONValue],
    execute: ReactToolExecutor,
) -> ReactToolDefinition:
    return ReactToolDefinition(
        name=name,
        description=description,
        request_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(required),
        },
        response_schema=react_tool_response_schema(success_schema=success_schema),
        execute=execute,
        concurrency=ToolConcurrency("parallel"),
    )


def build_web_research_definitions(
    *,
    web_search: ReactToolExecutor,
    web_extract: ReactToolExecutor,
) -> tuple[ReactToolDefinition, ...]:
    offset: dict[str, JSONValue] = {"type": "integer", "minimum": 1}
    return (
        _definition(
            name="web_search",
            description=(
                "Search the web for current evidence. Returns up to "
                f"{WEB_SEARCH_RESULT_LIMIT} results per query, each with a short "
                "excerpt. Continue result pages with next_offset; use web_extract "
                "for full page content."
            ),
            properties={
                "query": {"type": "string", "minLength": 1, "pattern": r"\S"},
                "offset": offset,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": WEB_SEARCH_MAX_RESULTS,
                },
            },
            required=("query", "offset", "limit"),
            success_schema=_web_search_schema(),
            execute=web_search,
        ),
        _definition(
            name="web_extract",
            description=(
                "Extract bounded content from specific web pages. Continue each "
                "URL with its next_offset. With a query, each page holds only its "
                f"top {WEB_EXCERPTS_PER_PAGE} excerpts relevant to the query; set "
                "query to null to read the full page."
            ),
            properties={
                "urls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": WEB_EXTRACT_MAX_URLS,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": WEB_URL_MAX_CHARS,
                    },
                },
                "query": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "pattern": r"\S",
                },
                "offset": offset,
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": WEB_EXTRACT_PAGE_MAX_CHARS,
                },
            },
            required=("urls", "query", "offset", "limit"),
            success_schema=_web_extract_schema(),
            execute=web_extract,
        ),
    )


def _web_search_schema() -> dict[str, JSONValue]:
    return _success_schema(
        properties={
            "query": {"type": "string"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "title",
                        "url",
                        "content",
                        "content_truncated",
                        "score",
                    ],
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "content": {"type": "string"},
                        "content_truncated": {"type": "boolean"},
                        "score": {"type": "number"},
                    },
                },
            },
            **_PAGE_PROPERTIES,
            "retry_hint": _NULLABLE_STRING,
        },
        required=(
            "query",
            "results",
            "next_offset",
            "truncated",
            "truncation_reason",
            "retry_hint",
        ),
    )


def _web_extract_schema() -> dict[str, JSONValue]:
    page_properties: dict[str, JSONValue] = {
        "url": {"type": "string"},
        "raw_content": {"type": "string"},
        "offset": {"type": "integer", "minimum": 1},
        "end_offset": {"type": "integer", "minimum": 0},
        "total_chars": {"type": "integer", "minimum": 0},
        **_PAGE_PROPERTIES,
        "retry_hint": _NULLABLE_STRING,
    }
    return _success_schema(
        properties={
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(page_properties),
                    "properties": page_properties,
                },
            },
            "failed_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["url", "error", "error_truncated"],
                    "properties": {
                        "url": {"type": "string"},
                        "error": {"type": "string"},
                        "error_truncated": {"type": "boolean"},
                    },
                },
            },
        },
        required=("results", "failed_results"),
    )


__all__ = [
    "WEB_EXTRACT_MAX_URLS",
    "WEB_EXTRACT_PAGE_MAX_CHARS",
    "WEB_SEARCH_MAX_RESULTS",
    "WEB_SEARCH_RESULT_CONTENT_MAX_CHARS",
    "build_web_research_definitions",
]

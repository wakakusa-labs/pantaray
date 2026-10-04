"""web_search ツール定義。"""

from __future__ import annotations

from pantaray_llm.profiles import (
    WEB_SEARCH_COUNTRIES,
    WEB_SEARCH_RESULT_LIMIT,
    WEB_SEARCH_TOPICS,
)

from .base import (
    InputSpec,
    ToolDefinition,
    ToolGuideSpec,
    ToolSpec,
    field_spec,
    tool_execution_policy,
)
from .web_content_validation import validate_web_search_args

WEB_SEARCH_TOOL = ToolDefinition.from_spec(
    ToolSpec(
        tool_id="web_search",
        name="Web Search",
        description=(
            "Search the web via the cloud search wrapper and return up to "
            f"{WEB_SEARCH_RESULT_LIMIT} results for one focused query. Each result's "
            "content is a short excerpt; use web_extract to read the full page."
        ),
        guide=ToolGuideSpec(
            what=(
                "Use this to look up external public information on the web and "
                "return normalized search evidence."
            ),
            when=(
                "Use when you need external/public facts not available in memory_search "
                "or the current context."
            ),
            pitfalls=(
                "Keep the query focused. Use topic only when it materially improves "
                "retrieval, and only set country for general web searches when "
                "regional coverage matters."
            ),
        ),
        execution_policy=tool_execution_policy(
            intent_class="network_access",
            default_timeout_ms=60_000,
        ),
        input_spec=InputSpec(
            fields=(
                field_spec(
                    name="query",
                    schema={
                        "type": "string",
                        "minLength": 1,
                        "pattern": r"\S",
                    },
                    required=True,
                    description="Focused Tavily search query to execute.",
                ),
                field_spec(
                    name="topic",
                    schema={
                        "type": "string",
                        "enum": list(WEB_SEARCH_TOPICS),
                    },
                    required=False,
                    description=(
                        "Optional Tavily search topic. Use general for broad web "
                        "search, news for recent reporting, or finance for market "
                        "and company information."
                    ),
                ),
                field_spec(
                    name="country",
                    schema={
                        "type": "string",
                        "enum": list(WEB_SEARCH_COUNTRIES),
                    },
                    required=False,
                    description=(
                        "Optional Tavily country hint. Only use this with the "
                        "general topic when regional results matter."
                    ),
                ),
            )
        ),
        output_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["success", "error"],
                },
                "query": {"type": "string"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "content": {"type": "string"},
                            "score": {"type": "number"},
                        },
                        "required": ["title", "url", "content", "score"],
                        "additionalProperties": False,
                    },
                },
                "images": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["url", "description"],
                        "additionalProperties": False,
                    },
                },
                "error": {
                    "type": "object",
                    "properties": {
                        "error_type": {"type": "string"},
                        "error_code": {"type": "string"},
                        "error_message": {"type": "string"},
                        "error_details": {"type": "object"},
                        "severity": {"type": "string"},
                        "metadata": {"type": "null"},
                    },
                    "required": [
                        "error_type",
                        "error_code",
                        "error_message",
                        "error_details",
                        "severity",
                        "metadata",
                    ],
                    "additionalProperties": False,
                },
                "meta": {
                    "type": "object",
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
                    "required": [
                        "request_id",
                        "upstream_provider",
                        "profile_id",
                        "outcome",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": ["status", "query", "results", "images"],
            "additionalProperties": False,
        },
        runtime_config={
            "max_retries": 3,
        },
        pre_validate_args=validate_web_search_args,
    )
)

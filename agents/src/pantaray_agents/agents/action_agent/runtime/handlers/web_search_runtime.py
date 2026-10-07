"""web_search ツール実行の専用ランタイム。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NotRequired, Required, TypedDict

from pantaray_agents.agents.action_agent.runtime.handlers.tool_args import (
    optional_string_arg,
)
from pantaray_agents.agents.action_agent.runtime.handlers.web_tool_error_mapper import (
    map_web_tool_error,
)
from pantaray_agents.agents.action_agent.tools import ToolDefinition
from pantaray_agents.local_runtime.web_tools.client import (
    WebContentInvalidResponseError,
    build_web_tools_wrapper_context,
    require_non_empty_string,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.web.fetch import WebSearchResponse, fetch_web_search
from pantaray_llm.errors import (
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_NO_RESULTS,
    PROXY_SUGGESTED_ACTION_REFINE_QUERY,
    ProxyAgentErrorPayload,
    ProxyToolResultMeta,
    build_proxy_tool_result_meta,
)
from pantaray_llm.profiles import WEB_SEARCH_PROFILE_ID

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState


class WebSearchResult(TypedDict, total=False):
    title: Required[str]
    url: Required[str]
    content: Required[str]
    score: Required[float]


class WebSearchImage(TypedDict):
    url: str
    description: str


class WebSearchPayload(TypedDict, total=False):
    status: Required[str]
    query: Required[str]
    results: Required[list[WebSearchResult]]
    images: Required[list[WebSearchImage]]
    meta: NotRequired[ProxyToolResultMeta]
    error: NotRequired[ProxyAgentErrorPayload]


@dataclass(slots=True)
class WebSearchExecutionOutcome:
    status: str
    payload: WebSearchPayload
    prompt_tokens: int
    completion_tokens: int


def _search_payload(response: WebSearchResponse) -> WebSearchPayload:
    return {
        "status": response.status,
        "query": response.query,
        "results": [
            {
                "title": row.title,
                "url": row.url,
                "content": row.content,
                "score": row.score,
            }
            for row in response.results
        ],
        "images": [
            {"url": image.url, "description": image.description}
            for image in response.images
        ],
    }


def _build_search_meta(
    *,
    payload: WebSearchPayload,
    request_id: str,
    upstream_request_id: str | None,
) -> ProxyToolResultMeta:
    if payload["results"] or payload["images"]:
        return build_proxy_tool_result_meta(
            request_id=request_id,
            upstream_provider="tavily",
            profile_id=WEB_SEARCH_PROFILE_ID,
            outcome=PROXY_OUTCOME_COMPLETE,
            upstream_request_id=upstream_request_id,
        )
    return build_proxy_tool_result_meta(
        request_id=request_id,
        upstream_provider="tavily",
        profile_id=WEB_SEARCH_PROFILE_ID,
        outcome=PROXY_OUTCOME_NO_RESULTS,
        upstream_request_id=upstream_request_id,
        suggested_action=PROXY_SUGGESTED_ACTION_REFINE_QUERY,
    )


async def run_web_search_tool(
    agent: ActionAgent,
    step_id: str,
    tool_def: ToolDefinition,
    args: dict[str, JSONValue],
    state: ActionAgentState,
) -> WebSearchExecutionOutcome:
    """web_search を実行し、cloud wrapper 由来の検索結果を返す。"""

    _ = agent

    query = require_non_empty_string(args.get("query"), field_name="query")
    runtime_cfg = tool_def.runtime_config or {}
    max_retries = int(runtime_cfg.get("max_retries", 3) or 3)
    request_context = build_web_tools_wrapper_context(
        user_id=state.get("user_id"),
        action_id=state.get("action_id"),
    )

    try:
        response = await fetch_web_search(
            context=request_context,
            query=query,
            max_retries=max_retries,
            topic=optional_string_arg(args, "topic"),
            country=optional_string_arg(args, "country"),
        )
        payload = _search_payload(response)
        payload["meta"] = _build_search_meta(
            payload=payload,
            request_id=request_context["request_id"],
            upstream_request_id=response.upstream_request_id,
        )
        status = "success" if payload["status"] == "success" else "error"
    except (WebContentInvalidResponseError, ValueError, RuntimeError) as exc:
        payload = {
            "status": "error",
            "query": query,
            "results": [],
            "images": [],
            "error": map_web_tool_error(
                tool_id="web_search",
                exc=exc,
                request_id=request_context["request_id"],
                profile_id=WEB_SEARCH_PROFILE_ID,
            ),
        }
        status = "error"

    return WebSearchExecutionOutcome(
        status=status,
        payload=payload,
        prompt_tokens=0,
        completion_tokens=0,
    )

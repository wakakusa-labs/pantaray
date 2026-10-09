"""web_crawl ツール実行の専用ランタイム。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NotRequired, Required, TypedDict

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
from pantaray_agents.tools.web.fetch import WebCrawlResponse, fetch_web_crawl
from pantaray_llm.errors import (
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_NO_RESULTS,
    PROXY_SUGGESTED_ACTION_CHANGE_URL,
    ProxyAgentErrorPayload,
    ProxyToolResultMeta,
    build_proxy_tool_result_meta,
)
from pantaray_llm.profiles import (
    WEB_CRAWL_MAX_BREADTH,
    WEB_CRAWL_MAX_DEPTH,
    WEB_CRAWL_PAGE_LIMIT,
    WEB_CRAWL_PROFILE_ID,
    WEB_EXCERPTS_PER_PAGE,
)

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState


class WebCrawlResult(TypedDict, total=False):
    url: Required[str]
    raw_content: Required[str]


class WebCrawlPayload(TypedDict, total=False):
    base_url: NotRequired[str]
    results: Required[list[WebCrawlResult]]
    retry_hint: NotRequired[str]
    response_time: NotRequired[float]
    meta: NotRequired[ProxyToolResultMeta]
    error: NotRequired[ProxyAgentErrorPayload]


@dataclass(slots=True)
class WebCrawlExecutionOutcome:
    status: str
    payload: WebCrawlPayload
    prompt_tokens: int
    completion_tokens: int


def _crawl_payload(response: WebCrawlResponse) -> WebCrawlPayload:
    payload: WebCrawlPayload = {
        "base_url": response.base_url,
        "results": [
            {"url": page.url, "raw_content": page.raw_content}
            for page in response.results
        ],
    }
    if response.response_time is not None:
        payload["response_time"] = response.response_time
    return payload


def _crawl_retry_hint(*, page_count: int, has_instructions: bool) -> str:
    """Say how far the crawl went; Tavily never reports what it skipped."""

    coverage = (
        f"it reached the {WEB_CRAWL_PAGE_LIMIT}-link limit, so pages it had not "
        "reached are missing"
        if page_count >= WEB_CRAWL_PAGE_LIMIT
        else "it does not report which links these limits skipped, so other "
        "pages in the section may be missing"
    )
    hint = (
        f"Returned {page_count} pages. The crawl follows at most "
        f"{WEB_CRAWL_MAX_BREADTH} links per page and {WEB_CRAWL_MAX_DEPTH} levels "
        f"deep and stops after {WEB_CRAWL_PAGE_LIMIT} links; {coverage}. To reach "
        "them, crawl again from a narrower section URL or use web_extract on "
        "specific URLs."
    )
    if has_instructions:
        hint += (
            " instructions was set, so each raw_content holds only up to "
            f"{WEB_EXCERPTS_PER_PAGE} excerpts relevant to it, joined by [...], "
            "not the full page. Use web_extract without query on a URL to read "
            "the full page."
        )
    return hint


def _build_crawl_meta(
    *,
    payload: WebCrawlPayload,
    request_id: str,
    upstream_request_id: str | None,
) -> ProxyToolResultMeta:
    if payload["results"]:
        return build_proxy_tool_result_meta(
            request_id=request_id,
            upstream_provider="tavily",
            profile_id=WEB_CRAWL_PROFILE_ID,
            outcome=PROXY_OUTCOME_COMPLETE,
            upstream_request_id=upstream_request_id,
        )
    return build_proxy_tool_result_meta(
        request_id=request_id,
        upstream_provider="tavily",
        profile_id=WEB_CRAWL_PROFILE_ID,
        outcome=PROXY_OUTCOME_NO_RESULTS,
        upstream_request_id=upstream_request_id,
        suggested_action=PROXY_SUGGESTED_ACTION_CHANGE_URL,
    )


async def run_web_crawl_tool(
    agent: ActionAgent,
    step_id: str,
    tool_def: ToolDefinition,
    args: dict[str, JSONValue],
    state: ActionAgentState,
) -> WebCrawlExecutionOutcome:
    """web_crawl を実行し、cloud wrapper 由来の本文一覧を返す。"""

    _ = agent

    url_value = args.get("url")
    url = require_non_empty_string(url_value, field_name="url")

    instructions_value = args.get("instructions")
    instructions = (
        require_non_empty_string(instructions_value, field_name="instructions")
        if instructions_value is not None
        else None
    )

    runtime_cfg = tool_def.runtime_config or {}
    max_retries = int(runtime_cfg.get("max_retries", 3) or 3)
    request_context = build_web_tools_wrapper_context(
        user_id=state.get("user_id"),
        action_id=state.get("action_id"),
    )
    try:
        response = await fetch_web_crawl(
            context=request_context,
            url=url,
            instructions=instructions,
            max_retries=max_retries,
        )
        payload = _crawl_payload(response)
        if payload["results"]:
            payload["retry_hint"] = _crawl_retry_hint(
                page_count=len(payload["results"]),
                has_instructions=instructions is not None,
            )
        payload["meta"] = _build_crawl_meta(
            payload=payload,
            request_id=request_context["request_id"],
            upstream_request_id=response.upstream_request_id,
        )
        status = "success"
    except (RuntimeError, WebContentInvalidResponseError, ValueError) as exc:
        payload = {
            "results": [],
            "error": map_web_tool_error(
                tool_id="web_crawl",
                exc=exc,
                request_id=request_context["request_id"],
                profile_id=WEB_CRAWL_PROFILE_ID,
            ),
        }
        status = "error"

    return WebCrawlExecutionOutcome(
        status=status,
        payload=payload,
        prompt_tokens=0,
        completion_tokens=0,
    )

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
    invoke_web_tools_wrapper,
    require_non_empty_string,
    require_non_empty_text,
)
from pantaray_agents.schema.agent.base import JSONValue
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


class _TavilyCrawlRow(TypedDict, total=False):
    url: str
    raw_content: str


class _TavilyCrawlResponse(TypedDict, total=False):
    base_url: str
    results: list[_TavilyCrawlRow]
    response_time: float
    request_id: str


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


def _coerce_crawl_row(value: object) -> _TavilyCrawlRow | None:
    if not isinstance(value, dict):
        return None
    row: _TavilyCrawlRow = {}
    if "url" in value:
        row["url"] = value.get("url")
    if "raw_content" in value:
        row["raw_content"] = value.get("raw_content")
    return row


def _coerce_crawl_response(value: object) -> _TavilyCrawlResponse | None:
    if not isinstance(value, dict):
        return None
    base_url = value.get("base_url")
    if not isinstance(base_url, str):
        return None
    if "results" not in value or not isinstance(value.get("results"), list):
        return None

    rows: list[_TavilyCrawlRow] = []
    for row in value["results"]:
        typed_row = _coerce_crawl_row(row)
        if typed_row is None:
            return None
        rows.append(typed_row)

    response: _TavilyCrawlResponse = {"base_url": base_url, "results": rows}
    response_time = value.get("response_time")
    if response_time is not None:
        if not isinstance(response_time, int | float):
            return None
        response["response_time"] = float(response_time)
    request_id = value.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str):
            return None
        response["request_id"] = request_id
    return response


def _normalize_crawl_rows(
    rows: list[_TavilyCrawlRow],
) -> list[WebCrawlResult]:
    return [
        {
            "url": require_non_empty_string(row.get("url"), field_name="url"),
            "raw_content": require_non_empty_text(
                row.get("raw_content"),
                field_name="content",
            ),
        }
        for row in rows
    ]


def _normalize_crawl_payload(
    response: _TavilyCrawlResponse,
) -> WebCrawlPayload:
    payload: WebCrawlPayload = {
        "base_url": require_non_empty_string(
            response.get("base_url"),
            field_name="base_url",
        ),
        "results": _normalize_crawl_rows(response["results"]),
    }
    response_time = response.get("response_time")
    if isinstance(response_time, float):
        payload["response_time"] = response_time
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
    wrapper_args: dict[str, JSONValue] = {"url": url}
    if instructions:
        wrapper_args["instructions"] = instructions

    try:
        parsed = _coerce_crawl_response(
            (
                await invoke_web_tools_wrapper(
                    tool_id="web_crawl",
                    web_tool_profile=WEB_CRAWL_PROFILE_ID,
                    args=wrapper_args,
                    context=request_context,
                    max_retries=max_retries,
                )
            ).get("result")
            or {}
        )
        if parsed is None:
            raise WebContentInvalidResponseError("response")
        payload = _normalize_crawl_payload(parsed)
        if payload["results"]:
            payload["retry_hint"] = _crawl_retry_hint(
                page_count=len(payload["results"]),
                has_instructions=instructions is not None,
            )
        payload["meta"] = _build_crawl_meta(
            payload=payload,
            request_id=request_context["request_id"],
            upstream_request_id=parsed.get("request_id"),
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

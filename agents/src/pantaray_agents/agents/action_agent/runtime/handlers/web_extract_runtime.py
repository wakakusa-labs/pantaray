"""web_extract ツール実行の専用ランタイム。"""

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
from pantaray_agents.tools.web.fetch import WebExtractResponse, fetch_web_extract
from pantaray_llm.errors import (
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_NO_RESULTS,
    PROXY_OUTCOME_PARTIAL_RESULTS,
    PROXY_SUGGESTED_ACTION_CHANGE_URL,
    ProxyAgentErrorPayload,
    ProxyToolResultMeta,
    build_proxy_tool_result_meta,
)
from pantaray_llm.profiles import WEB_EXCERPTS_PER_PAGE, WEB_EXTRACT_PROFILE_ID

if TYPE_CHECKING:  # pragma: no cover
    from pantaray_agents.agents.action_agent import ActionAgent
    from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState

QUERY_EXCERPTS_RETRY_HINT = (
    f"query was set, so each raw_content holds only up to {WEB_EXCERPTS_PER_PAGE} "
    "excerpts relevant to it, joined by [...], not the full page. Call "
    "web_extract again without query to read the full page."
)


class WebExtractResult(TypedDict, total=False):
    url: Required[str]
    raw_content: Required[str]


class WebExtractFailedResult(TypedDict, total=False):
    url: Required[str]
    error: Required[str]


class WebExtractPayload(TypedDict, total=False):
    results: Required[list[WebExtractResult]]
    failed_results: Required[list[WebExtractFailedResult]]
    truncated: NotRequired[bool]
    retry_hint: NotRequired[str | None]
    response_time: NotRequired[float]
    meta: NotRequired[ProxyToolResultMeta]
    error: NotRequired[ProxyAgentErrorPayload]


@dataclass(slots=True)
class WebExtractExecutionOutcome:
    status: str
    payload: WebExtractPayload
    prompt_tokens: int
    completion_tokens: int


def _extract_payload(response: WebExtractResponse) -> WebExtractPayload:
    payload: WebExtractPayload = {
        "results": [
            {"url": page.url, "raw_content": page.raw_content}
            for page in response.results
        ],
        "failed_results": [
            {"url": failure.url, "error": failure.error}
            for failure in response.failed_results
        ],
    }
    if response.response_time is not None:
        payload["response_time"] = response.response_time
    return payload


def _build_extract_meta(
    *,
    payload: WebExtractPayload,
    request_id: str,
    upstream_request_id: str | None,
) -> ProxyToolResultMeta:
    if payload["results"] and payload["failed_results"]:
        return build_proxy_tool_result_meta(
            request_id=request_id,
            upstream_provider="tavily",
            profile_id=WEB_EXTRACT_PROFILE_ID,
            outcome=PROXY_OUTCOME_PARTIAL_RESULTS,
            upstream_request_id=upstream_request_id,
        )
    if payload["results"] or payload["failed_results"]:
        return build_proxy_tool_result_meta(
            request_id=request_id,
            upstream_provider="tavily",
            profile_id=WEB_EXTRACT_PROFILE_ID,
            outcome=PROXY_OUTCOME_COMPLETE,
            upstream_request_id=upstream_request_id,
        )
    return build_proxy_tool_result_meta(
        request_id=request_id,
        upstream_provider="tavily",
        profile_id=WEB_EXTRACT_PROFILE_ID,
        outcome=PROXY_OUTCOME_NO_RESULTS,
        upstream_request_id=upstream_request_id,
        suggested_action=PROXY_SUGGESTED_ACTION_CHANGE_URL,
    )


def _aggregate_web_extract_status(payload: WebExtractPayload) -> str:
    if payload.get("results") or payload.get("failed_results"):
        return "success"
    if payload.get("error"):
        return "error"
    return "success"


async def run_web_extract_tool(
    agent: ActionAgent,
    step_id: str,
    tool_def: ToolDefinition,
    args: dict[str, JSONValue],
    state: ActionAgentState,
) -> WebExtractExecutionOutcome:
    """web_extract を実行し、cloud wrapper 由来の本文一覧を返す。"""

    _ = agent

    urls_value = args.get("urls") or []
    if not isinstance(urls_value, list):
        raise ValueError("urls must be an array")
    urls = [require_non_empty_string(value, field_name="url") for value in urls_value]
    query_value = args.get("query")
    query = (
        require_non_empty_string(query_value, field_name="query")
        if query_value is not None
        else None
    )

    runtime_cfg = tool_def.runtime_config or {}
    max_retries = int(runtime_cfg.get("max_retries", 3) or 3)
    request_context = build_web_tools_wrapper_context(
        user_id=state.get("user_id"),
        action_id=state.get("action_id"),
    )

    try:
        response = await fetch_web_extract(
            context=request_context,
            urls=urls,
            query=query,
            max_retries=max_retries,
        )
        payload = _extract_payload(response)
        payload["truncated"] = query is not None and bool(payload["results"])
        payload["retry_hint"] = (
            QUERY_EXCERPTS_RETRY_HINT if payload["truncated"] else None
        )
        payload["meta"] = _build_extract_meta(
            payload=payload,
            request_id=request_context["request_id"],
            upstream_request_id=response.upstream_request_id,
        )
        status = _aggregate_web_extract_status(payload)
    except (RuntimeError, WebContentInvalidResponseError, ValueError) as exc:
        payload = {
            "results": [],
            "failed_results": [],
            "error": map_web_tool_error(
                tool_id="web_extract",
                exc=exc,
                request_id=request_context["request_id"],
                profile_id=WEB_EXTRACT_PROFILE_ID,
            ),
        }
        status = "error"

    return WebExtractExecutionOutcome(
        status=status,
        payload=payload,
        prompt_tokens=0,
        completion_tokens=0,
    )

from __future__ import annotations

import asyncio
import logging
import time
from typing import cast

from tavily import TavilyClient
from tavily.errors import (
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    MissingAPIKeyError,
    UsageLimitExceededError,
)
from tavily.errors import (
    TimeoutError as TavilyTimeoutError,
)

from pantaray_llm.contracts.json_value import JSONValue
from pantaray_llm.errors import (
    PROXY_AUTHENTICATION_FAILED,
    PROXY_INVALID_INPUT,
    PROXY_INVALID_UPSTREAM_RESPONSE,
    PROXY_OUTCOME_COMPLETE,
    PROXY_OUTCOME_NO_RESULTS,
    PROXY_OUTCOME_PARTIAL_RESULTS,
    PROXY_REQUEST_FAILED,
    PROXY_UPSTREAM_FORBIDDEN,
    PROXY_UPSTREAM_RATE_LIMITED,
    PROXY_UPSTREAM_UNAVAILABLE,
    ProviderError,
    ProxyProvider,
    ProxyToolResultMeta,
    build_proxy_tool_result_meta,
)
from pantaray_llm.web_tools.profiles import (
    WebCrawlProfile,
    WebExtractProfile,
    WebSearchProfile,
    get_web_tool_profile,
)
from pantaray_llm.web_tools.schemas import (
    WebCrawlArgs,
    WebExtractArgs,
    WebSearchArgs,
    WebToolsProxyRequest,
    WebToolsProxyResponse,
)

UPSTREAM_PROVIDER_TAVILY: ProxyProvider = "tavily"
logger = logging.getLogger(__name__)


def _meta_payload(meta: ProxyToolResultMeta) -> dict[str, JSONValue]:
    # ProxyToolResultMeta is a TypedDict of JSON scalars; the tool payload type is
    # the JSON value contract, so the wrapper only changes the static type.
    return cast(dict[str, JSONValue], meta)


def _require_string_arg(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message=f"Tavily returned an invalid {field_name}.",
        )
    return value.strip()


def _error_details(
    *,
    request_id: str,
    profile_id: str,
    upstream_request_id: str | None = None,
) -> dict[str, JSONValue]:
    details: dict[str, JSONValue] = {
        "request_id": request_id,
        "upstream_provider": UPSTREAM_PROVIDER_TAVILY,
        "profile_id": profile_id,
    }
    if upstream_request_id is not None:
        details["upstream_request_id"] = upstream_request_id
    return details


def _raise_tavily_error(
    *,
    tool_id: str,
    request_id: str,
    profile_id: str,
    exc: Exception,
) -> None:
    details = _error_details(request_id=request_id, profile_id=profile_id)
    if isinstance(exc, (MissingAPIKeyError, InvalidAPIKeyError)):
        raise ProviderError(
            status_code=502,
            code=PROXY_AUTHENTICATION_FAILED,
            message="The web tool service authentication failed.",
            details=details,
        ) from exc
    if isinstance(exc, UsageLimitExceededError):
        raise ProviderError(
            status_code=429,
            code=PROXY_UPSTREAM_RATE_LIMITED,
            message="The external provider is rate-limited.",
            details=details,
        ) from exc
    if isinstance(exc, ForbiddenError):
        raise ProviderError(
            status_code=403,
            code=PROXY_UPSTREAM_FORBIDDEN,
            message="The target content could not be accessed.",
            details=details,
        ) from exc
    if isinstance(exc, BadRequestError):
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=f"{tool_id} arguments were rejected by the provider.",
            details=details,
        ) from exc
    if isinstance(exc, TavilyTimeoutError):
        raise ProviderError(
            status_code=503,
            code=PROXY_UPSTREAM_UNAVAILABLE,
            message="The external provider timed out.",
            details=details,
        ) from exc
    logger.exception("Tavily request failed: tool_id=%s", tool_id)
    raise ProviderError(
        status_code=502,
        code=PROXY_REQUEST_FAILED,
        message=f"{tool_id} request failed before a valid upstream response was returned.",
        details=details,
    ) from exc


def _coerce_search_result_rows(
    result: object,
) -> tuple[list[JSONValue], list[JSONValue]]:
    if not isinstance(result, dict):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Tavily search returned an invalid payload.",
        )
    raw_results = result.get("results")
    raw_images = result.get("images")
    if raw_results is not None and not isinstance(raw_results, list):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Tavily search results must be a list.",
        )
    if raw_images is not None and not isinstance(raw_images, list):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Tavily search images must be a list.",
        )

    results: list[JSONValue] = []
    for row in raw_results or []:
        if not isinstance(row, dict):
            raise ProviderError(
                status_code=502,
                code=PROXY_INVALID_UPSTREAM_RESPONSE,
                message="Tavily search result row must be an object.",
            )
        score = row.get("score")
        if not isinstance(score, int | float) or isinstance(score, bool):
            raise ProviderError(
                status_code=502,
                code=PROXY_INVALID_UPSTREAM_RESPONSE,
                message="Tavily search score must be numeric.",
            )
        results.append(
            {
                "title": _require_string_arg(row.get("title"), field_name="title"),
                "url": _require_string_arg(row.get("url"), field_name="url"),
                "content": _require_string_arg(
                    row.get("content"),
                    field_name="content",
                ),
                "score": float(score),
            }
        )

    images: list[JSONValue] = []
    for row in raw_images or []:
        if not isinstance(row, dict):
            raise ProviderError(
                status_code=502,
                code=PROXY_INVALID_UPSTREAM_RESPONSE,
                message="Tavily image row must be an object.",
            )
        images.append(
            {
                "url": _require_string_arg(row.get("url"), field_name="url"),
                "description": _require_string_arg(
                    row.get("description"),
                    field_name="description",
                ),
            }
        )
    return results, images


def _coerce_extract_result(result: object) -> dict[str, JSONValue]:
    if not isinstance(result, dict):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Tavily extract returned an invalid payload.",
        )
    rows = result.get("results")
    failed_rows = result.get("failed_results")
    if rows is not None and not isinstance(rows, list):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Extract results must be a list.",
        )
    if failed_rows is not None and not isinstance(failed_rows, list):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Extract failed_results must be a list.",
        )

    results: list[JSONValue] = []
    for row in rows or []:
        if not isinstance(row, dict):
            raise ProviderError(
                status_code=502,
                code=PROXY_INVALID_UPSTREAM_RESPONSE,
                message="Extract result row must be an object.",
            )
        results.append(
            {
                "url": _require_string_arg(row.get("url"), field_name="url"),
                "raw_content": _require_string_arg(
                    row.get("raw_content"),
                    field_name="raw_content",
                ),
            }
        )

    failed_results: list[JSONValue] = []
    for row in failed_rows or []:
        if not isinstance(row, dict):
            raise ProviderError(
                status_code=502,
                code=PROXY_INVALID_UPSTREAM_RESPONSE,
                message="Extract failed row must be an object.",
            )
        failed_results.append(
            {
                "url": _require_string_arg(row.get("url"), field_name="url"),
                "error": _require_string_arg(row.get("error"), field_name="error"),
            }
        )

    payload: dict[str, JSONValue] = {
        "results": results,
        "failed_results": failed_results,
    }
    response_time = result.get("response_time")
    if isinstance(response_time, int | float):
        payload["response_time"] = float(response_time)
    request_id = result.get("request_id")
    if isinstance(request_id, str) and request_id.strip():
        payload["request_id"] = request_id.strip()
    return payload


def _coerce_crawl_result(result: object) -> dict[str, JSONValue]:
    if not isinstance(result, dict):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Tavily crawl returned an invalid payload.",
        )
    base_url = _require_string_arg(result.get("base_url"), field_name="base_url")
    rows = result.get("results")
    if not isinstance(rows, list):
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message="Tavily crawl results must be a list.",
        )

    results: list[JSONValue] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError(
                status_code=502,
                code=PROXY_INVALID_UPSTREAM_RESPONSE,
                message="Tavily crawl row must be an object.",
            )
        results.append(
            {
                "url": _require_string_arg(row.get("url"), field_name="url"),
                "raw_content": _require_string_arg(
                    row.get("raw_content"),
                    field_name="raw_content",
                ),
            }
        )

    payload: dict[str, JSONValue] = {"base_url": base_url, "results": results}
    response_time = result.get("response_time")
    if isinstance(response_time, int | float):
        payload["response_time"] = float(response_time)
    request_id = result.get("request_id")
    if isinstance(request_id, str) and request_id.strip():
        payload["request_id"] = request_id.strip()
    return payload


async def _run_web_search(
    client: TavilyClient,
    *,
    request: WebToolsProxyRequest,
    profile: WebSearchProfile,
) -> dict[str, JSONValue]:
    if not isinstance(request.args, WebSearchArgs):
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message="web_search args are invalid.",
            details=_error_details(
                request_id=request.request_context.request_id,
                profile_id=request.web_tool_profile,
            ),
        )
    resolved_topic = request.args.topic or profile.default_topic
    search_kwargs: dict[str, object] = {
        "query": request.args.query,
        "search_depth": profile.search_depth,
        "topic": resolved_topic,
        "max_results": profile.max_results,
    }
    if request.args.country is not None:
        search_kwargs["country"] = request.args.country

    try:
        result = await asyncio.to_thread(client.search, **search_kwargs)
    except Exception as exc:  # noqa: BLE001
        _raise_tavily_error(
            tool_id="web_search",
            request_id=request.request_context.request_id,
            profile_id=request.web_tool_profile,
            exc=exc,
        )
    results, images = _coerce_search_result_rows(result)
    raw_request_id = result.get("request_id") if isinstance(result, dict) else None
    upstream_request_id = (
        raw_request_id.strip()
        if isinstance(raw_request_id, str) and raw_request_id.strip()
        else None
    )
    return {
        "status": "success",
        "query": request.args.query,
        "results": results,
        "images": images,
        "meta": _meta_payload(
            build_proxy_tool_result_meta(
                request_id=request.request_context.request_id,
                upstream_provider=UPSTREAM_PROVIDER_TAVILY,
                profile_id=request.web_tool_profile,
                outcome=(
                    PROXY_OUTCOME_COMPLETE
                    if results or images
                    else PROXY_OUTCOME_NO_RESULTS
                ),
                upstream_request_id=upstream_request_id,
            )
        ),
    }


async def _run_web_extract(
    client: TavilyClient,
    *,
    request: WebToolsProxyRequest,
    profile: WebExtractProfile,
) -> dict[str, JSONValue]:
    if not isinstance(request.args, WebExtractArgs):
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message="web_extract args are invalid.",
            details=_error_details(
                request_id=request.request_context.request_id,
                profile_id=request.web_tool_profile,
            ),
        )
    extract_kwargs: dict[str, object] = {
        "urls": request.args.urls,
        "extract_depth": profile.extract_depth,
    }
    if request.args.query is not None:
        # Tavily then returns only the top excerpts per page, not the full page.
        extract_kwargs["query"] = request.args.query
        extract_kwargs["chunks_per_source"] = profile.query_chunks_per_source
    try:
        result = await asyncio.to_thread(client.extract, **extract_kwargs)
    except Exception as exc:  # noqa: BLE001
        _raise_tavily_error(
            tool_id="web_extract",
            request_id=request.request_context.request_id,
            profile_id=request.web_tool_profile,
            exc=exc,
        )
    payload = _coerce_extract_result(result)
    results = payload.get("results")
    failed_results = payload.get("failed_results")
    outcome = (
        PROXY_OUTCOME_PARTIAL_RESULTS
        if isinstance(results, list)
        and isinstance(failed_results, list)
        and results
        and failed_results
        else PROXY_OUTCOME_COMPLETE
        if (isinstance(results, list) and results)
        or (isinstance(failed_results, list) and failed_results)
        else PROXY_OUTCOME_NO_RESULTS
    )
    extract_request_id = payload.get("request_id")
    payload["meta"] = _meta_payload(
        build_proxy_tool_result_meta(
            request_id=request.request_context.request_id,
            upstream_provider=UPSTREAM_PROVIDER_TAVILY,
            profile_id=request.web_tool_profile,
            outcome=outcome,
            upstream_request_id=(
                extract_request_id if isinstance(extract_request_id, str) else None
            ),
        )
    )
    return payload


async def _run_web_crawl(
    client: TavilyClient,
    *,
    request: WebToolsProxyRequest,
    profile: WebCrawlProfile,
) -> dict[str, JSONValue]:
    if not isinstance(request.args, WebCrawlArgs):
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message="web_crawl args are invalid.",
            details=_error_details(
                request_id=request.request_context.request_id,
                profile_id=request.web_tool_profile,
            ),
        )
    crawl_kwargs: dict[str, object] = {
        "url": request.args.url,
        "max_depth": profile.max_depth,
        "max_breadth": profile.max_breadth,
        "limit": profile.limit,
        "extract_depth": profile.extract_depth,
    }
    if request.args.instructions is not None:
        # Tavily then returns only the top excerpts per page, not the full page.
        crawl_kwargs["instructions"] = request.args.instructions
        crawl_kwargs["chunks_per_source"] = profile.instructions_chunks_per_source
    try:
        result = await asyncio.to_thread(client.crawl, **crawl_kwargs)
    except Exception as exc:  # noqa: BLE001
        _raise_tavily_error(
            tool_id="web_crawl",
            request_id=request.request_context.request_id,
            profile_id=request.web_tool_profile,
            exc=exc,
        )
    payload = _coerce_crawl_result(result)
    rows = payload.get("results")
    crawl_request_id = payload.get("request_id")
    payload["meta"] = _meta_payload(
        build_proxy_tool_result_meta(
            request_id=request.request_context.request_id,
            upstream_provider=UPSTREAM_PROVIDER_TAVILY,
            profile_id=request.web_tool_profile,
            outcome=(
                PROXY_OUTCOME_COMPLETE
                if isinstance(rows, list) and rows
                else PROXY_OUTCOME_NO_RESULTS
            ),
            upstream_request_id=(
                crawl_request_id if isinstance(crawl_request_id, str) else None
            ),
        )
    )
    return payload


async def execute_web_tools_proxy_request(
    request: WebToolsProxyRequest,
    *,
    api_key: str,
) -> WebToolsProxyResponse:
    """Run one web tool against Tavily with the key the caller supplies.

    Where the key comes from is the caller's business: Pantaray Cloud reads
    the operator's key from its environment, while the local runtime keeps the
    user's own key in memory and never puts it in one.
    """
    profile = get_web_tool_profile(request.web_tool_profile)
    if profile.tool_id != request.tool_id:
        raise ProviderError(
            status_code=400,
            code=PROXY_INVALID_INPUT,
            message=(
                "web_tool_profile does not match tool_id: "
                f"{request.web_tool_profile} -> {profile.tool_id}"
            ),
            details=_error_details(
                request_id=request.request_context.request_id,
                profile_id=request.web_tool_profile,
            ),
        )

    client = TavilyClient(api_key)
    started_at = time.perf_counter()
    try:
        if request.tool_id == "web_search":
            if not isinstance(profile, WebSearchProfile):
                raise AssertionError("web_search profile type mismatch")
            result = await _run_web_search(client, request=request, profile=profile)
        elif request.tool_id == "web_extract":
            if not isinstance(profile, WebExtractProfile):
                raise AssertionError("web_extract profile type mismatch")
            result = await _run_web_extract(client, request=request, profile=profile)
        else:
            if not isinstance(profile, WebCrawlProfile):
                raise AssertionError("web_crawl profile type mismatch")
            result = await _run_web_crawl(client, request=request, profile=profile)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Unexpected web tools proxy failure: tool_id=%s", request.tool_id
        )
        raise ProviderError(
            status_code=502,
            code=PROXY_INVALID_UPSTREAM_RESPONSE,
            message=f"{request.tool_id} returned an invalid upstream response.",
            details=_error_details(
                request_id=request.request_context.request_id,
                profile_id=request.web_tool_profile,
            ),
        ) from exc

    return WebToolsProxyResponse(
        tool_id=request.tool_id,
        status="success",
        request_id=request.request_context.request_id,
        response_time_ms=int((time.perf_counter() - started_at) * 1000),
        result=result,
    )

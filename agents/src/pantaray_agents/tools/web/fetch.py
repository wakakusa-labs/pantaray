"""Call the web tools wrapper and check its result, once for every agent."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pantaray_agents.local_runtime.web_tools.client import (
    WebContentInvalidResponseError,
    WebToolsWrapperContext,
    invoke_web_tools_wrapper,
    require_non_empty_text,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_llm.profiles import (
    WEB_CRAWL_PROFILE_ID,
    WEB_EXTRACT_PROFILE_ID,
    WEB_SEARCH_PROFILE_ID,
)


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    content: str
    score: float


@dataclass(frozen=True, slots=True)
class WebSearchImage:
    url: str
    description: str


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    status: str
    query: str
    results: tuple[WebSearchResult, ...]
    images: tuple[WebSearchImage, ...]
    upstream_request_id: str | None


@dataclass(frozen=True, slots=True)
class WebPage:
    url: str
    raw_content: str


@dataclass(frozen=True, slots=True)
class WebExtractFailure:
    url: str
    error: str


@dataclass(frozen=True, slots=True)
class WebExtractResponse:
    results: tuple[WebPage, ...]
    failed_results: tuple[WebExtractFailure, ...]
    response_time: float | None
    upstream_request_id: str | None


@dataclass(frozen=True, slots=True)
class WebCrawlResponse:
    base_url: str
    results: tuple[WebPage, ...]
    response_time: float | None
    upstream_request_id: str | None


async def fetch_web_search(
    *,
    context: WebToolsWrapperContext,
    query: str,
    max_retries: int,
    topic: str | None = None,
    country: str | None = None,
) -> WebSearchResponse:
    result = await _invoke(
        "web_search",
        WEB_SEARCH_PROFILE_ID,
        {"query": query, "topic": topic, "country": country},
        context=context,
        max_retries=max_retries,
    )
    if _text(result, "query") != query:
        raise WebContentInvalidResponseError("query")
    if result.get("error") is not None:
        raise WebContentInvalidResponseError("error")
    meta = result.get("meta") or {}
    if not isinstance(meta, dict):
        raise WebContentInvalidResponseError("meta")
    has_upstream_request_id = meta.get("upstream_request_id") is not None
    return WebSearchResponse(
        status=_text(result, "status"),
        query=query,
        results=tuple(
            WebSearchResult(
                score=_score(row),
                title=_text(row, "title"),
                url=_text(row, "url"),
                content=_text(row, "content"),
            )
            for row in _rows(result.get("results"), field="results")
        ),
        images=tuple(
            WebSearchImage(url=_text(row, "url"), description=_text(row, "description"))
            for row in _rows(result.get("images"), field="images")
        ),
        upstream_request_id=(
            _text(meta, "upstream_request_id") if has_upstream_request_id else None
        ),
    )


async def fetch_web_extract(
    *,
    context: WebToolsWrapperContext,
    urls: Sequence[str],
    query: str | None,
    max_retries: int,
) -> WebExtractResponse:
    result = await _invoke(
        "web_extract",
        WEB_EXTRACT_PROFILE_ID,
        {"urls": list(urls), "query": query},
        context=context,
        max_retries=max_retries,
    )
    return WebExtractResponse(
        results=_pages(result.get("results")),
        failed_results=tuple(
            WebExtractFailure(url=_text(row, "url"), error=_text(row, "error"))
            for row in _rows(result.get("failed_results"), field="failed_results")
        ),
        response_time=_response_time(result),
        upstream_request_id=_upstream_request_id(result),
    )


async def fetch_web_crawl(
    *,
    context: WebToolsWrapperContext,
    url: str,
    instructions: str | None,
    max_retries: int,
) -> WebCrawlResponse:
    result = await _invoke(
        "web_crawl",
        WEB_CRAWL_PROFILE_ID,
        {"url": url, "instructions": instructions},
        context=context,
        max_retries=max_retries,
    )
    return WebCrawlResponse(
        base_url=_text(result, "base_url"),
        results=_pages(result.get("results")),
        response_time=_response_time(result),
        upstream_request_id=_upstream_request_id(result),
    )


async def _invoke(
    tool_id: str,
    profile_id: str,
    args: dict[str, JSONValue],
    *,
    context: WebToolsWrapperContext,
    max_retries: int,
) -> dict[str, JSONValue]:
    response = await invoke_web_tools_wrapper(
        tool_id=tool_id,
        web_tool_profile=profile_id,
        args={key: value for key, value in args.items() if value is not None},
        context=context,
        max_retries=max_retries,
    )
    result = response.get("result")
    if result is None:
        raise WebContentInvalidResponseError("result")
    return result


def _rows(value: JSONValue, *, field: str) -> list[dict[str, JSONValue]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise WebContentInvalidResponseError(field)
    return [row for row in value if isinstance(row, dict)]


def _pages(value: JSONValue) -> tuple[WebPage, ...]:
    return tuple(
        WebPage(url=_text(row, "url"), raw_content=_text(row, "raw_content"))
        for row in _rows(value, field="results")
    )


def _score(row: Mapping[str, JSONValue]) -> float:
    score = row.get("score")
    if not isinstance(score, int | float) or isinstance(score, bool):
        raise WebContentInvalidResponseError("score")
    return float(score)


def _text(row: Mapping[str, JSONValue], key: str) -> str:
    return require_non_empty_text(row.get(key), field_name=key)


def _response_time(result: dict[str, JSONValue]) -> float | None:
    value = result.get("response_time")
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise WebContentInvalidResponseError("response_time")
    return float(value)


def _upstream_request_id(result: dict[str, JSONValue]) -> str | None:
    value = result.get("request_id")
    if value is not None and not isinstance(value, str):
        raise WebContentInvalidResponseError("request_id")
    return value

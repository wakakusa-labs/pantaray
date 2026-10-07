from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pantaray_agents.local_runtime.web_tools import WebContentInvalidResponseError
from pantaray_agents.schema.agent.base import JSONValue

from .definitions import WEB_SEARCH_RESULT_CONTENT_MAX_CHARS

WEB_EXTRACT_FAILED_RESULT_MAX_CHARS = 300
WEB_EXTRACT_QUERY_EXCERPTS_HINT = (
    "Only excerpts relevant to query were returned, not the full page; call "
    "web_extract with query=null to read the full page."
)


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    content: str
    score: float


@dataclass(frozen=True, slots=True)
class WebSearchSnapshot:
    query: str
    results: tuple[WebSearchResult, ...]


@dataclass(frozen=True, slots=True)
class WebExtractFailure:
    url: str
    error: str


def parse_web_search_snapshot(
    *, wrapper_result: Mapping[str, JSONValue], expected_query: str
) -> WebSearchSnapshot:
    query = _required_string(wrapper_result.get("query"), field="query")
    if query != expected_query:
        raise WebContentInvalidResponseError("query")
    raw_results = wrapper_result.get("results")
    if not isinstance(raw_results, list):
        raise WebContentInvalidResponseError("results")
    results: list[WebSearchResult] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise WebContentInvalidResponseError("results")
        score = raw_result.get("score")
        if not isinstance(score, int | float) or isinstance(score, bool):
            raise WebContentInvalidResponseError("score")
        results.append(
            WebSearchResult(
                title=_required_string(raw_result.get("title"), field="title"),
                url=_required_string(raw_result.get("url"), field="url"),
                content=_required_string(raw_result.get("content"), field="content"),
                score=float(score),
            )
        )
    return WebSearchSnapshot(query=query, results=tuple(results))


def web_search_page(
    *, snapshot: WebSearchSnapshot, offset: int, limit: int
) -> dict[str, JSONValue]:
    _validate_page(offset=offset, limit=limit)
    start = offset - 1
    selected = snapshot.results[start : start + limit]
    rows: list[JSONValue] = []
    has_truncated_content = False
    for result in selected:
        content = result.content[:WEB_SEARCH_RESULT_CONTENT_MAX_CHARS]
        content_truncated = len(content) < len(result.content)
        has_truncated_content = has_truncated_content or content_truncated
        rows.append(
            {
                "title": result.title,
                "url": result.url,
                "content": content,
                "content_truncated": content_truncated,
                "score": result.score,
            }
        )
    candidate_next_offset = start + len(selected) + 1
    next_offset: int | None = (
        candidate_next_offset
        if candidate_next_offset <= len(snapshot.results)
        else None
    )
    truncated = next_offset is not None or has_truncated_content
    if next_offset is not None:
        truncation_reason = "page_limit"
        retry_hint = "Continue with offset=next_offset."
    elif has_truncated_content:
        truncation_reason = "result_content_limit"
        retry_hint = "Use web_extract with the result URL to read the page content."
    else:
        truncation_reason = None
        retry_hint = None
    return {
        "status": "success",
        "query": snapshot.query,
        "results": rows,
        "next_offset": next_offset,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "retry_hint": retry_hint,
    }


def parse_web_extract_result(
    *, wrapper_result: Mapping[str, JSONValue]
) -> tuple[dict[str, str], tuple[WebExtractFailure, ...]]:
    raw_results = wrapper_result.get("results")
    raw_failures = wrapper_result.get("failed_results")
    if not isinstance(raw_results, list) or not isinstance(raw_failures, list):
        raise WebContentInvalidResponseError("web_extract result")
    contents: dict[str, str] = {}
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise WebContentInvalidResponseError("results")
        url = _required_string(raw_result.get("url"), field="url")
        content = _required_string(raw_result.get("raw_content"), field="raw_content")
        if url in contents:
            raise WebContentInvalidResponseError("duplicate result URL")
        contents[url] = content
    failures: list[WebExtractFailure] = []
    for raw_failure in raw_failures:
        if not isinstance(raw_failure, dict):
            raise WebContentInvalidResponseError("failed_results")
        failures.append(
            WebExtractFailure(
                url=_required_string(raw_failure.get("url"), field="url"),
                error=_required_string(raw_failure.get("error"), field="error"),
            )
        )
    return contents, tuple(failures)


def web_extract_pages(
    *,
    urls: tuple[str, ...],
    query: str | None,
    offset: int,
    limit: int,
    snapshots: Mapping[tuple[str, str | None], str],
    failures: tuple[WebExtractFailure, ...],
) -> dict[str, JSONValue]:
    _validate_page(offset=offset, limit=limit)
    failures_by_url = {failure.url: failure for failure in failures}
    results: list[JSONValue] = []
    failed_results: list[JSONValue] = []
    for url in urls:
        content = snapshots.get((url, query))
        if content is None:
            failure = failures_by_url.get(url)
            error = failure.error if failure is not None else "No content was returned."
            failed_results.append(_bounded_failure(url=url, error=error))
            continue
        start = offset - 1
        if start >= len(content) and not (start == 0 and not content):
            failed_results.append(
                _bounded_failure(
                    url=url,
                    error=(
                        f"Offset {offset} is out of range for this page "
                        f"({len(content)} characters)."
                    ),
                )
            )
            continue
        page = content[start : start + limit]
        end_offset = start + len(page)
        next_offset = end_offset + 1 if end_offset < len(content) else None
        hints: list[str] = []
        if next_offset is not None:
            hints.append("Continue this URL with offset=next_offset.")
        if query is not None:
            hints.append(WEB_EXTRACT_QUERY_EXCERPTS_HINT)
        results.append(
            {
                "url": url,
                "raw_content": page,
                "offset": offset,
                "end_offset": end_offset,
                "total_chars": len(content),
                "next_offset": next_offset,
                "truncated": bool(hints),
                # next_offset already shows paging, so name the larger cut.
                "truncation_reason": (
                    "query_excerpts"
                    if query is not None
                    else "page_limit"
                    if next_offset is not None
                    else None
                ),
                "retry_hint": " ".join(hints) or None,
            }
        )
    return {
        "status": "success",
        "results": results,
        "failed_results": failed_results,
    }


def _bounded_failure(*, url: str, error: str) -> dict[str, JSONValue]:
    bounded = error[:WEB_EXTRACT_FAILED_RESULT_MAX_CHARS]
    return {
        "url": url,
        "error": bounded,
        "error_truncated": len(bounded) < len(error),
    }


def _required_string(value: JSONValue, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebContentInvalidResponseError(field)
    return value


def _validate_page(*, offset: int, limit: int) -> None:
    if offset <= 0 or limit <= 0:
        raise ValueError("offset and limit must be positive")


__all__ = [
    "WebExtractFailure",
    "WebSearchSnapshot",
    "parse_web_extract_result",
    "parse_web_search_snapshot",
    "web_extract_pages",
    "web_search_page",
]

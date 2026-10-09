from __future__ import annotations

from collections.abc import Mapping

from pantaray_agents.schema.agent.base import JSONValue

from .definitions import WEB_SEARCH_RESULT_CONTENT_MAX_CHARS
from .fetch import WebExtractFailure, WebSearchResponse

WEB_EXTRACT_QUERY_EXCERPTS_HINT = (
    "Only excerpts relevant to query were returned, not the full page; call "
    "web_extract with query=null to read the full page."
)


def web_search_page(
    *, snapshot: WebSearchResponse, offset: int, limit: int
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
    failed_urls = {failure.url for failure in failures}
    results: list[JSONValue] = []
    failed_results: list[JSONValue] = []
    for url in urls:
        content = snapshots.get((url, query))
        if content is None:
            # The provider's own words for a page it could not read are not
            # passed on: the user is told only that the page was not read.
            failed_results.append(
                {
                    "url": url,
                    "error": "This page could not be read."
                    if url in failed_urls
                    else "No content was returned.",
                }
            )
            continue
        start = offset - 1
        if start >= len(content) and not (start == 0 and not content):
            failed_results.append(
                {
                    "url": url,
                    "error": (
                        f"Offset {offset} is out of range for this page "
                        f"({len(content)} characters)."
                    ),
                }
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


def _validate_page(*, offset: int, limit: int) -> None:
    if offset <= 0 or limit <= 0:
        raise ValueError("offset and limit must be positive")


__all__ = [
    "web_extract_pages",
    "web_search_page",
]

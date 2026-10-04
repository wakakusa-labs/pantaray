from __future__ import annotations

import pytest

from pantaray_llm.profiles import (
    WEB_CRAWL_MAX_BREADTH,
    WEB_CRAWL_MAX_DEPTH,
    WEB_CRAWL_PAGE_LIMIT,
    WEB_CRAWL_PROFILE_ID,
    WEB_EXCERPTS_PER_PAGE,
    WEB_EXTRACT_PROFILE_ID,
    WEB_SEARCH_PROFILE_ID,
    WEB_SEARCH_RESULT_LIMIT,
)
from pantaray_llm.web_tools.schemas import WebToolsProxyRequest
from pantaray_llm.web_tools.tavily import execute_web_tools_proxy_request

# The tool descriptions state these limits, so each call must send them instead
# of leaving them to whatever default the Tavily API applies.


class _RecordingTavilyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def search(self, **kwargs: object) -> object:
        self.calls.append(("search", kwargs))
        return {"results": [], "images": []}

    def extract(self, **kwargs: object) -> object:
        self.calls.append(("extract", kwargs))
        return {"results": [], "failed_results": []}

    def crawl(self, **kwargs: object) -> object:
        self.calls.append(("crawl", kwargs))
        return {"base_url": "https://example.test/docs", "results": []}


async def _call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_id: str,
    profile_id: str,
    args: dict[str, object],
) -> dict[str, object]:
    client = _RecordingTavilyClient()
    monkeypatch.setattr(
        "pantaray_llm.web_tools.tavily.TavilyClient", lambda _api_key: client
    )
    await execute_web_tools_proxy_request(
        WebToolsProxyRequest.model_validate(
            {
                "tool_id": tool_id,
                "web_tool_profile": profile_id,
                "args": args,
                "request_context": {"user_id": "user-1", "request_id": "request-1"},
            }
        ),
        api_key="test-key",
    )
    [(_name, kwargs)] = client.calls
    return kwargs


@pytest.mark.asyncio
async def test_web_search_sends_its_result_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = await _call(
        monkeypatch,
        tool_id="web_search",
        profile_id=WEB_SEARCH_PROFILE_ID,
        args={"query": "Pantaray"},
    )

    assert kwargs["max_results"] == WEB_SEARCH_RESULT_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, "pricing"])
async def test_web_extract_sends_its_excerpt_count_only_with_a_query(
    monkeypatch: pytest.MonkeyPatch, query: str | None
) -> None:
    args: dict[str, object] = {"urls": ["https://example.test/a"]}
    if query is not None:
        args["query"] = query

    kwargs = await _call(
        monkeypatch,
        tool_id="web_extract",
        profile_id=WEB_EXTRACT_PROFILE_ID,
        args=args,
    )

    assert kwargs.get("chunks_per_source") == (
        WEB_EXCERPTS_PER_PAGE if query is not None else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("instructions", [None, "Only API pages"])
async def test_web_crawl_sends_its_page_limits(
    monkeypatch: pytest.MonkeyPatch, instructions: str | None
) -> None:
    args: dict[str, object] = {"url": "https://example.test/docs"}
    if instructions is not None:
        args["instructions"] = instructions

    kwargs = await _call(
        monkeypatch,
        tool_id="web_crawl",
        profile_id=WEB_CRAWL_PROFILE_ID,
        args=args,
    )

    assert kwargs["max_depth"] == WEB_CRAWL_MAX_DEPTH
    assert kwargs["max_breadth"] == WEB_CRAWL_MAX_BREADTH
    assert kwargs["limit"] == WEB_CRAWL_PAGE_LIMIT
    assert kwargs.get("chunks_per_source") == (
        WEB_EXCERPTS_PER_PAGE if instructions is not None else None
    )

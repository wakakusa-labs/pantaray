from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    ToolValidationError,
    _run_validated_tool_impl,
    _validate_tool_args,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools.web_extract_tool import WEB_EXTRACT_TOOL
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.tools.web.execute import QUERY_EXCERPTS_RETRY_HINT

_FETCH = "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper"


async def _run_tool(args: dict[str, Any]):
    validated_args = dict(args)
    _validate_tool_args(WEB_EXTRACT_TOOL, validated_args)
    runtime = SimpleNamespace(services=SimpleNamespace(token_accounting=MagicMock()))
    return await _run_validated_tool_impl(
        MagicMock(),
        WEB_EXTRACT_TOOL,
        validated_args,
        create_initial_state(
            user_id="user-123",
            suggestion_id="sug-123",
            action_id="act-123",
            started_at="2025-01-01T00:00:00Z",
            max_steps=10,
            max_tool_steps=10,
            token_budget=None,
        ),
        sink=CountingSink(),
        runtime=runtime,
    )


def _pages(provider: str | None = None) -> AsyncMock:
    result: dict[str, object] = {
        "results": [{"url": "https://e.com/a", "raw_content": "body"}],
        "failed_results": [{"url": "https://e.com/b", "error": "HTTP 403 Forbidden"}],
    }
    if provider is not None:
        result["meta"] = {"upstream_provider": provider}
    return AsyncMock(return_value={"result": result})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "urls",
    [
        [f"https://e.com/{index}" for index in range(6)],
        ["http://localhost:8000/"],
    ],
    ids=["too-many", "local"],
)
async def test_web_extract_refuses_urls_it_will_not_read(urls: list[str]) -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool({"urls": urls})


@pytest.mark.asyncio
async def test_web_extract_reports_each_page_and_each_failure_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FETCH, _pages())

    result = await _run_tool({"urls": ["https://e.com/a", "https://e.com/b"]})

    assert result.status == "success"
    assert result.output["results"] == [
        {"url": "https://e.com/a", "raw_content": "body"}
    ]
    assert result.output["failed_results"] == [
        {"url": "https://e.com/b", "error": "HTTP 403 Forbidden"}
    ]
    assert result.output["retry_hint"] is None
    assert result.output["meta"]["outcome"] == "partial_results"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "hint"),
    [(None, QUERY_EXCERPTS_RETRY_HINT), ("local", None)],
    ids=["provider-excerpts", "read-here-whole"],
)
async def test_web_extract_says_excerpts_only_when_the_route_made_them(
    monkeypatch: pytest.MonkeyPatch, provider: str | None, hint: str | None
) -> None:
    monkeypatch.setattr(_FETCH, _pages(provider))

    result = await _run_tool({"urls": ["https://e.com/a"], "query": "pricing"})

    assert result.output["retry_hint"] == hint
    assert result.output["meta"]["upstream_provider"] == (provider or "tavily")


@pytest.mark.asyncio
async def test_web_extract_names_a_requested_url_the_provider_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _FETCH,
        AsyncMock(
            return_value={
                "result": {
                    "results": [{"url": "https://e.com/a", "raw_content": "body"}],
                    "failed_results": [],
                }
            }
        ),
    )

    result = await _run_tool({"urls": ["https://e.com/a", "https://e.com/b"]})

    assert result.output["results"] == [
        {"url": "https://e.com/a", "raw_content": "body"}
    ]
    assert result.output["failed_results"] == [
        {"url": "https://e.com/b", "error": "No content was returned for this URL."}
    ]
    assert result.output["meta"]["outcome"] == "partial_results"

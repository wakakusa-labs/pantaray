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
from pantaray_agents.agents.action_agent.tools.web_search_tool import WEB_SEARCH_TOOL
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.local_runtime.web_tools.client import WebContentExecutionError

_FETCH = "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper"


def _base_state() -> dict[str, Any]:
    return create_initial_state(
        user_id="user-123",
        suggestion_id="sug-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )


async def _run_tool(args: dict[str, Any]):
    validated_args = dict(args)
    _validate_tool_args(WEB_SEARCH_TOOL, validated_args)
    runtime = SimpleNamespace(services=SimpleNamespace(token_accounting=MagicMock()))
    return await _run_validated_tool_impl(
        MagicMock(),
        WEB_SEARCH_TOOL,
        validated_args,
        _base_state(),
        sink=CountingSink(),
        runtime=runtime,
    )


def _provider_result(query: str, results: list[dict[str, object]]) -> dict[str, Any]:
    return {
        "result": {
            "query": query,
            "results": results,
            "meta": {"upstream_request_id": "provider-1"},
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [{"query": "   "}, {"query": "chips", "topic": "news", "country": "japan"}],
    ids=["blank-query", "country-outside-general"],
)
async def test_web_search_refuses_a_request_the_provider_cannot_serve(
    args: dict[str, Any],
) -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(args)


@pytest.mark.asyncio
async def test_web_search_returns_each_result_whole_with_its_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"title": "T", "url": "https://e.com/a", "content": "C" * 3_000, "score": 0.9}
    invoke = AsyncMock(return_value=_provider_result("q", [row]))
    monkeypatch.setattr(_FETCH, invoke)

    result = await _run_tool({"query": "q", "topic": "news"})

    assert result.status == "success"
    assert result.output["results"] == [row]
    assert result.output["meta"]["upstream_provider"] == "tavily"
    assert result.output["meta"]["upstream_request_id"] == "provider-1"
    assert invoke.await_args.kwargs["args"] == {"query": "q", "topic": "news"}
    assert invoke.await_args.kwargs["max_retries"] == 3


@pytest.mark.asyncio
async def test_web_search_with_no_results_suggests_refining_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FETCH, AsyncMock(return_value=_provider_result("q", [])))

    result = await _run_tool({"query": "q"})

    assert result.status == "success"
    assert result.output["meta"]["outcome"] == "no_results"
    assert result.output["meta"]["suggested_action"] == "refine_query"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        AsyncMock(
            return_value=_provider_result(
                "q", [{"title": "T", "content": "C", "score": 0.9}]
            )
        ),
        AsyncMock(
            side_effect=WebContentExecutionError(
                error_code="PROXY_REQUEST_FAILED",
                error_message="The web tool request failed.",
                retryable=True,
            )
        ),
    ],
    ids=["invalid-row", "transport"],
)
async def test_web_search_failure_names_no_provider_detail(
    monkeypatch: pytest.MonkeyPatch, failure: AsyncMock
) -> None:
    monkeypatch.setattr(_FETCH, failure)

    result = await _run_tool({"query": "q"})

    assert result.status == "error"
    assert result.output == {
        "status": "error",
        "error_code": "WEB_TOOL_FAILED",
        "message": "Web search is unavailable right now, so nothing was looked up.",
    }

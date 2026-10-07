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
from pantaray_agents.agents.action_agent.runtime.handlers.web_search_runtime import (
    run_web_search_tool,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools.web_search_tool import WEB_SEARCH_TOOL
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.local_runtime.web_tools.client import (
    WebContentExecutionError,
)


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


async def _run_tool(*, args: dict[str, Any], state: dict[str, Any]):
    validated_args = dict(args)
    _validate_tool_args(WEB_SEARCH_TOOL, validated_args)
    runtime = SimpleNamespace(services=SimpleNamespace(token_accounting=MagicMock()))
    return await _run_validated_tool_impl(
        MagicMock(),
        WEB_SEARCH_TOOL,
        validated_args,
        state,
        sink=CountingSink(),
        runtime=runtime,
    )


def _success_wrapper_payload(query: str) -> dict[str, object]:
    return {
        "tool_id": "web_search",
        "status": "ok",
        "request_id": "req-1",
        "result": {
            "status": "success",
            "query": query,
            "results": [
                {
                    "title": f"Title {query}",
                    "url": f"https://example.com/{query}",
                    "content": f"Content {query}",
                    "score": 0.9,
                }
            ],
            "meta": {
                "request_id": "req-1",
                "upstream_provider": "tavily",
                "profile_id": "web_search.default",
                "outcome": "complete",
                "upstream_request_id": "provider-1",
            },
            "images": [
                {
                    "url": f"https://img.example.com/{query}.png",
                    "description": f"Image {query}",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_web_search_rejects_blank_query() -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(args={"query": "   "}, state=_base_state())


@pytest.mark.asyncio
async def test_web_search_rejects_country_for_news_topic() -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(
            args={
                "query": "latest semiconductor news",
                "topic": "news",
                "country": "japan",
            },
            state=_base_state(),
        )


@pytest.mark.asyncio
async def test_web_search_runtime_fail_closes_on_non_string_query() -> None:
    with pytest.raises(ValueError, match="query"):
        await run_web_search_tool(
            agent=MagicMock(),
            step_id="step-1",
            tool_def=WEB_SEARCH_TOOL,
            args={"query": 1},
            state=_base_state(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_web_search_returns_normalized_results_and_images(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_invoke(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return _success_wrapper_payload("test-query")

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={
            "query": "test-query",
            "topic": "general",
            "country": "japan",
        },
        state=_base_state(),
    )

    assert result.status == "success"
    assert captured["tool_id"] == "web_search"
    assert captured["args"] == {
        "query": "test-query",
        "topic": "general",
        "country": "japan",
    }
    assert result.output == {
        "status": "success",
        "query": "test-query",
        "results": [
            {
                "title": "Title test-query",
                "url": "https://example.com/test-query",
                "content": "Content test-query",
                "score": 0.9,
            }
        ],
        "images": [
            {
                "url": "https://img.example.com/test-query.png",
                "description": "Image test-query",
            }
        ],
        "meta": {
            "request_id": result.output["meta"]["request_id"],
            "upstream_provider": "tavily",
            "profile_id": "web_search.default",
            "outcome": "complete",
            "upstream_request_id": "provider-1",
        },
    }


@pytest.mark.asyncio
async def test_web_search_success_with_empty_results(monkeypatch) -> None:
    async def _fake_invoke(**_kwargs: object) -> dict[str, object]:
        return {
            "tool_id": "web_search",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "status": "success",
                "query": "q",
                "results": [],
                "images": [],
                "meta": {
                    "request_id": "req-1",
                    "upstream_provider": "tavily",
                    "profile_id": "web_search.default",
                    "outcome": "no_results",
                    "upstream_request_id": "provider-1",
                },
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(args={"query": "q"}, state=_base_state())

    assert result.status == "success"
    assert result.output["results"] == []
    assert result.output["images"] == []
    assert result.output["meta"]["outcome"] == "no_results"
    assert result.output["meta"]["suggested_action"] == "refine_query"
    assert result.output["meta"]["upstream_request_id"] == "provider-1"


@pytest.mark.asyncio
async def test_web_search_invalid_result_row_maps_to_error(monkeypatch) -> None:
    async def _fake_invoke(**_kwargs: object) -> dict[str, object]:
        return {
            "tool_id": "web_search",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "status": "success",
                "query": "q",
                "results": [{"title": "T", "content": "C", "score": 0.9}],
                "images": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(args={"query": "q"}, state=_base_state())

    assert result.status == "error"
    assert result.output["error"]["error_code"] == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert result.output["error"]["error_message"] == (
        "The provider returned an invalid response. Do not reuse this result; "
        "retry later or switch strategy."
    )
    assert result.output["error"]["error_details"]["suggested_action"] == "switch_tool"
    assert result.output["error"]["error_details"]["surface_id"] == "web_search"


@pytest.mark.asyncio
async def test_web_search_transport_failure_maps_to_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        AsyncMock(
            side_effect=WebContentExecutionError(
                error_code="PROXY_REQUEST_FAILED",
                error_message="The web tool request failed before a response was returned.",
                retryable=True,
            )
        ),
    )

    result = await _run_tool(
        args={"query": "q1"},
        state=_base_state(),
    )

    assert result.status == "error"
    assert result.output["error"]["error_code"] == "PROXY_REQUEST_FAILED"
    assert result.output["error"]["error_details"]["suggested_action"] == "retry_later"
    assert result.output["query"] == "q1"
    assert result.output["results"] == []
    assert result.output["images"] == []
    assert result.output["error"]["error_details"]["surface_id"] == "web_search"
    assert result.output["error"]["error_details"]["retryable"] is True
    assert result.output["error"]["error_details"]["request_id"]
    assert result.output["error"]["error_details"]["profile_id"] == "web_search.default"
    assert result.output["error"]["error_details"]["upstream_provider"] == "tavily"

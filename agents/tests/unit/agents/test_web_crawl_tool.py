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
from pantaray_agents.agents.action_agent.tools.web_crawl_tool import WEB_CRAWL_TOOL
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.local_runtime.web_tools.client import (
    WebContentExecutionError,
)
from pantaray_llm.profiles import WEB_CRAWL_PAGE_LIMIT


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
    _validate_tool_args(WEB_CRAWL_TOOL, validated_args)
    runtime = SimpleNamespace(services=SimpleNamespace(token_accounting=MagicMock()))
    return await _run_validated_tool_impl(
        MagicMock(),
        WEB_CRAWL_TOOL,
        validated_args,
        state,
        sink=CountingSink(),
        runtime=runtime,
    )


@pytest.mark.asyncio
async def test_web_crawl_rejects_invalid_url() -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(args={"url": "not-a-url"}, state=_base_state())


@pytest.mark.asyncio
async def test_web_crawl_rejects_blank_instructions() -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(
            args={"url": "https://example.com", "instructions": "   "},
            state=_base_state(),
        )


@pytest.mark.asyncio
async def test_web_crawl_returns_normalized_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_invoke(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "tool_id": "web_crawl",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "base_url": "https://example.com/docs",
                "results": [
                    {
                        "url": "https://example.com/a",
                        "raw_content": "abcdef",
                    },
                    {
                        "url": "https://example.com/b",
                        "raw_content": "uvwxyz",
                    },
                ],
                "response_time": 15.0,
                "request_id": "provider-1",
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={
            "url": "https://example.com/docs",
            "instructions": "Only docs pages",
        },
        state=_base_state(),
    )

    assert result.status == "success"
    assert result.output["retry_hint"].startswith("Returned 2 pages.")
    assert "may be missing" in result.output["retry_hint"]
    assert "holds only up to 3 excerpts" in result.output["retry_hint"]
    assert captured["tool_id"] == "web_crawl"
    assert captured["args"] == {
        "url": "https://example.com/docs",
        "instructions": "Only docs pages",
    }
    assert result.output == {
        "base_url": "https://example.com/docs",
        "results": [
            {
                "url": "https://example.com/a",
                "raw_content": "abcdef",
            },
            {
                "url": "https://example.com/b",
                "raw_content": "uvwxyz",
            },
        ],
        "retry_hint": result.output["retry_hint"],
        "response_time": 15.0,
        "meta": {
            "request_id": result.output["meta"]["request_id"],
            "upstream_provider": "tavily",
            "profile_id": "web_crawl.default",
            "outcome": "complete",
            "upstream_request_id": "provider-1",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_count", "coverage"),
    [
        (1, "may be missing"),
        (WEB_CRAWL_PAGE_LIMIT - 1, "may be missing"),
        (WEB_CRAWL_PAGE_LIMIT, "reached the 50-link limit"),
    ],
)
async def test_web_crawl_never_presents_its_pages_as_the_whole_section(
    monkeypatch, page_count: int, coverage: str
) -> None:
    async def _fake_invoke(**_kwargs: object) -> dict[str, object]:
        return {
            "tool_id": "web_crawl",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "base_url": "https://example.com/docs",
                "results": [
                    {"url": f"https://example.com/{index}", "raw_content": "page"}
                    for index in range(page_count)
                ],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={"url": "https://example.com/docs"},
        state=_base_state(),
    )

    hint = result.output["retry_hint"]
    assert result.status == "success"
    assert hint.startswith(f"Returned {page_count} pages.")
    assert coverage in hint
    assert "narrower section URL" in hint
    assert "excerpts" not in hint


@pytest.mark.asyncio
async def test_web_crawl_invalid_response_maps_to_error(monkeypatch) -> None:
    async def _fake_invoke(**_kwargs: object) -> dict[str, object]:
        return {
            "tool_id": "web_crawl",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "results": [
                    {
                        "url": "https://example.com/a",
                        "raw_content": "abcdef",
                    }
                ]
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={"url": "https://example.com/docs"},
        state=_base_state(),
    )

    assert result.status == "error"
    assert result.output["error"]["error_code"] == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert result.output["error"]["error_details"]["surface_id"] == "web_crawl"
    assert result.output["error"]["error_details"]["retryable"] is True
    assert result.output["error"]["error_details"]["suggested_action"] == "switch_tool"
    assert result.output["error"]["error_details"]["request_id"]
    assert result.output["error"]["error_details"]["profile_id"] == "web_crawl.default"
    assert result.output["error"]["error_details"]["upstream_provider"] == "tavily"


@pytest.mark.asyncio
async def test_web_crawl_transport_failure_maps_to_error(monkeypatch) -> None:
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
        args={"url": "https://example.com/docs"},
        state=_base_state(),
    )

    assert result.status == "error"
    assert result.output["results"] == []
    assert result.output["error"]["error_code"] == "PROXY_REQUEST_FAILED"
    assert result.output["error"]["error_details"]["surface_id"] == "web_crawl"
    assert result.output["error"]["error_details"]["retryable"] is True
    assert result.output["error"]["error_details"]["suggested_action"] == "retry_later"
    assert result.output["error"]["error_details"]["request_id"]
    assert result.output["error"]["error_details"]["profile_id"] == "web_crawl.default"
    assert result.output["error"]["error_details"]["upstream_provider"] == "tavily"

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
from pantaray_agents.agents.action_agent.runtime.handlers.web_extract_runtime import (
    QUERY_EXCERPTS_RETRY_HINT,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools.web_extract_tool import WEB_EXTRACT_TOOL
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
    _validate_tool_args(WEB_EXTRACT_TOOL, validated_args)
    runtime = SimpleNamespace(services=SimpleNamespace(token_accounting=MagicMock()))
    return await _run_validated_tool_impl(
        MagicMock(),
        WEB_EXTRACT_TOOL,
        validated_args,
        state,
        sink=CountingSink(),
        runtime=runtime,
    )


@pytest.mark.asyncio
async def test_web_extract_rejects_more_than_five_urls() -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(
            args={
                "urls": [
                    "https://example.com/1",
                    "https://example.com/2",
                    "https://example.com/3",
                    "https://example.com/4",
                    "https://example.com/5",
                    "https://example.com/6",
                ]
            },
            state=_base_state(),
        )


@pytest.mark.asyncio
async def test_web_extract_rejects_non_public_url() -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(
            args={"urls": ["http://localhost/private"]},
            state=_base_state(),
        )


@pytest.mark.asyncio
async def test_web_extract_returns_normalized_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_invoke(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "tool_id": "web_extract",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "results": [
                    {
                        "url": "https://example.com/a",
                        "raw_content": "  line 1\nline 2\n",
                    }
                ],
                "failed_results": [
                    {
                        "url": "https://example.com/b",
                        "error": "Timed out",
                    }
                ],
                "response_time": 12.5,
                "request_id": "provider-1",
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.web_extract_runtime.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={
            "urls": ["https://example.com/a", "https://example.com/b"],
            "query": "pricing details",
        },
        state=_base_state(),
    )

    assert result.status == "success"
    assert captured["tool_id"] == "web_extract"
    assert captured["args"] == {
        "urls": ["https://example.com/a", "https://example.com/b"],
        "query": "pricing details",
    }
    assert result.output == {
        "results": [
            {
                "url": "https://example.com/a",
                "raw_content": "  line 1\nline 2\n",
            }
        ],
        "failed_results": [
            {
                "url": "https://example.com/b",
                "error": "Timed out",
            }
        ],
        "truncated": True,
        "retry_hint": QUERY_EXCERPTS_RETRY_HINT,
        "response_time": 12.5,
        "meta": {
            "request_id": result.output["meta"]["request_id"],
            "upstream_provider": "tavily",
            "profile_id": "web_extract.default",
            "outcome": "partial_results",
            "upstream_request_id": "provider-1",
        },
    }


@pytest.mark.asyncio
async def test_web_extract_without_query_reports_full_pages(monkeypatch) -> None:
    async def _fake_invoke(**_kwargs: object) -> dict[str, object]:
        return {
            "tool_id": "web_extract",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "results": [{"url": "https://example.com/a", "raw_content": "body"}],
                "failed_results": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.web_extract_runtime.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={"urls": ["https://example.com/a"]},
        state=_base_state(),
    )

    assert result.status == "success"
    assert result.output["truncated"] is False
    assert result.output["retry_hint"] is None


@pytest.mark.asyncio
async def test_web_extract_invalid_response_maps_to_error(monkeypatch) -> None:
    async def _fake_invoke(**_kwargs: object) -> dict[str, object]:
        return {
            "tool_id": "web_extract",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "results": [{"url": "https://example.com/a"}],
                "failed_results": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.web_extract_runtime.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    result = await _run_tool(
        args={"urls": ["https://example.com/a"]},
        state=_base_state(),
    )

    assert result.status == "error"
    assert result.output["error"]["error_code"] == "PROXY_INVALID_UPSTREAM_RESPONSE"
    assert result.output["error"]["error_details"]["surface_id"] == "web_extract"
    assert result.output["error"]["error_details"]["retryable"] is True
    assert result.output["error"]["error_details"]["suggested_action"] == "switch_tool"
    assert result.output["error"]["error_details"]["request_id"]
    assert (
        result.output["error"]["error_details"]["profile_id"] == "web_extract.default"
    )
    assert result.output["error"]["error_details"]["upstream_provider"] == "tavily"


@pytest.mark.asyncio
async def test_web_extract_transport_failure_maps_to_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.web_extract_runtime.invoke_web_tools_wrapper",
        AsyncMock(
            side_effect=WebContentExecutionError(
                error_code="PROXY_REQUEST_FAILED",
                error_message="The web tool request failed before a response was returned.",
                retryable=True,
            )
        ),
    )

    result = await _run_tool(
        args={"urls": ["https://example.com/a"]},
        state=_base_state(),
    )

    assert result.status == "error"
    assert result.output["results"] == []
    assert result.output["failed_results"] == []
    assert result.output["error"]["error_code"] == "PROXY_REQUEST_FAILED"
    assert result.output["error"]["error_details"]["surface_id"] == "web_extract"
    assert result.output["error"]["error_details"]["retryable"] is True
    assert result.output["error"]["error_details"]["suggested_action"] == "retry_later"
    assert result.output["error"]["error_details"]["request_id"]
    assert (
        result.output["error"]["error_details"]["profile_id"] == "web_extract.default"
    )
    assert result.output["error"]["error_details"]["upstream_provider"] == "tavily"

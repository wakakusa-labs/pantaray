from __future__ import annotations

import asyncio
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
    [
        {"queries": ["   "]},
        {"queries": [f"q{index}" for index in range(6)]},
        {"queries": ["chips"], "topic": "news", "country": "japan"},
    ],
    ids=["blank-query", "six-queries", "country-outside-general"],
)
async def test_web_search_refuses_a_request_it_cannot_serve(
    args: dict[str, Any],
) -> None:
    with pytest.raises(ToolValidationError):
        await _run_tool(args)


def _row(url: str) -> dict[str, object]:
    return {"title": "T", "url": url, "content": "C" * 3_000, "score": 0.9}


@pytest.mark.asyncio
async def test_web_search_runs_each_query_side_by_side_and_reports_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both_started = asyncio.Event()
    started: list[str] = []

    async def invoke(**kwargs: Any) -> dict[str, Any]:
        query = kwargs["args"]["query"]
        started.append(query)
        if len(started) == 3:
            both_started.set()
        # Every search waits for the others: run one at a time, this never ends.
        await asyncio.wait_for(both_started.wait(), 5)
        if query == "down":
            raise WebContentExecutionError(
                error_code="PROXY_UPSTREAM_UNAVAILABLE",
                error_message="unavailable",
                retryable=False,
            )
        shared = _row("https://e.com/shared")
        own = _row(f"https://e.com/{query}")
        return _provider_result(query, [shared, own] if query == "b" else [own, shared])

    monkeypatch.setattr(_FETCH, invoke)

    result = await _run_tool({"queries": ["a", "down", "b"], "topic": "news"})

    assert result.status == "success"
    first, failed, second = result.output["searches"]
    assert [row["url"] for row in first["results"]] == [
        "https://e.com/a",
        "https://e.com/shared",
    ]
    # Found again by a later query, a URL stays only under the first.
    assert [row["url"] for row in second["results"]] == ["https://e.com/b"]
    assert second["meta"]["outcome"] == "complete"
    assert failed == {
        "query": "down",
        "error": "Web search is unavailable right now, so nothing was looked up.",
    }
    assert first["meta"]["request_id"] != second["meta"]["request_id"]


@pytest.mark.asyncio
async def test_web_search_with_no_results_suggests_refining_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FETCH, AsyncMock(return_value=_provider_result("q", [])))

    result = await _run_tool({"queries": ["q"]})

    meta = result.output["searches"][0]["meta"]
    assert meta["outcome"] == "no_results"
    assert meta["suggested_action"] == "refine_query"


@pytest.mark.asyncio
async def test_web_search_fails_as_a_whole_only_when_every_query_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _FETCH,
        AsyncMock(
            side_effect=WebContentExecutionError(
                error_code="PROXY_REQUEST_FAILED",
                error_message="The web tool request failed.",
                retryable=True,
            )
        ),
    )

    result = await _run_tool({"queries": ["q1", "q2"]})

    assert result.status == "error"
    assert result.output == {
        "status": "error",
        "error_code": "WEB_TOOL_FAILED",
        "message": "Web search is unavailable right now, so nothing was looked up.",
    }


@pytest.mark.asyncio
async def test_a_query_past_its_own_deadline_leaves_the_others_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.tools.web import execute

    monkeypatch.setattr(execute, "WEB_SEARCH_QUERY_SECONDS", 0.2)

    async def invoke(**kwargs: Any) -> dict[str, Any]:
        query = kwargs["args"]["query"]
        if query == "slow":
            # A provider still retrying when the call's own limit would fall.
            await asyncio.sleep(30)
        return _provider_result(query, [_row(f"https://e.com/{query}")])

    monkeypatch.setattr(_FETCH, invoke)

    result = await asyncio.wait_for(_run_tool({"queries": ["fast", "slow"]}), 5)

    found, late = result.output["searches"]
    assert [row["url"] for row in found["results"]] == ["https://e.com/fast"]
    assert late == {
        "query": "slow",
        "error": "The search did not finish within 0.2 seconds, so nothing was "
        "looked up.",
    }

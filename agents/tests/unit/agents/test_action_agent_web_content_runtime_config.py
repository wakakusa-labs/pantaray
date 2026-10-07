from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers import tools as tools_handler
from pantaray_agents.agents.action_agent.tools.web_crawl_tool import WEB_CRAWL_TOOL
from pantaray_agents.agents.action_agent.tools.web_extract_tool import WEB_EXTRACT_TOOL


@pytest.mark.asyncio
async def test_web_extract_runtime_config_passes_max_retries_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                        "url": "https://example.com/1",
                        "raw_content": "content-1",
                    }
                ],
                "failed_results": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    tool_def = replace(
        WEB_EXTRACT_TOOL,
        runtime_config={
            **(WEB_EXTRACT_TOOL.runtime_config or {}),
            "max_retries": 5,
        },
    )

    result = await tools_handler._run_web_extract(  # type: ignore[attr-defined]
        agent=MagicMock(),
        step_id="step",
        tool_def=tool_def,
        args={"urls": ["https://example.com/1"], "query": "pricing details"},
        state={"user_id": "user-1", "action_id": "act-1", "context": {}},  # type: ignore[arg-type]
    )

    assert result.output["results"][0]["url"] == "https://example.com/1"
    assert captured["max_retries"] == 5
    assert captured["web_tool_profile"] == "web_extract.default"
    assert captured["args"] == {
        "urls": ["https://example.com/1"],
        "query": "pricing details",
    }


@pytest.mark.asyncio
async def test_web_crawl_runtime_config_passes_max_retries_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                        "url": "https://example.com/docs/a",
                        "raw_content": "content-a",
                    }
                ],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    tool_def = replace(
        WEB_CRAWL_TOOL,
        runtime_config={
            **(WEB_CRAWL_TOOL.runtime_config or {}),
            "max_retries": 4,
        },
    )

    result = await tools_handler._run_web_crawl(  # type: ignore[attr-defined]
        agent=MagicMock(),
        step_id="step",
        tool_def=tool_def,
        args={"url": "https://example.com/docs"},
        state={"user_id": "user-1", "action_id": "act-1", "context": {}},  # type: ignore[arg-type]
    )

    assert result.output["base_url"] == "https://example.com/docs"
    assert captured["max_retries"] == 4
    assert captured["web_tool_profile"] == "web_crawl.default"

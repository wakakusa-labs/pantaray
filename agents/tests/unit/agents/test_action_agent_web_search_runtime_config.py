from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from pantaray_agents.agents.action_agent.agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers import tools as tools_handler
from pantaray_agents.agents.action_agent.tools.web_search_tool import WEB_SEARCH_TOOL
from pantaray_agents.agents.core import ToolLlmRunner


def test_action_agent_create_tool_llm_runner_returns_public_runner() -> None:
    agent = object.__new__(ActionAgent)
    agent.client = MagicMock()
    agent.llm_config = {}

    runner = agent.create_tool_llm_runner(tool_id="web_search")

    assert isinstance(runner, ToolLlmRunner)
    assert runner.DEFAULT_SYSTEM_INSTRUCTION == agent.DEFAULT_SYSTEM_INSTRUCTION
    assert runner.get_error_code_prefix() == "ACTION_WEB_SEARCH"


@pytest.mark.asyncio
async def test_web_search_runtime_config_passes_max_retries_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_invoke(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "tool_id": "web_search",
            "status": "ok",
            "request_id": "req-1",
            "result": {
                "status": "success",
                "query": "q1",
                "results": [],
                "images": [],
            },
        }

    monkeypatch.setattr(
        "pantaray_agents.tools.web.fetch.invoke_web_tools_wrapper",
        _fake_invoke,
    )

    tool_def = replace(
        WEB_SEARCH_TOOL,
        runtime_config={
            **(WEB_SEARCH_TOOL.runtime_config or {}),
            "max_retries": 7,
        },
    )

    result = await tools_handler._run_web_search(  # type: ignore[attr-defined]
        agent=MagicMock(),
        step_id="step",
        tool_def=tool_def,
        args={"query": "q1"},
        state={"user_id": "user-1", "action_id": "act-1", "context": {}},  # type: ignore[arg-type]
    )

    assert result.output["query"] == "q1"
    assert captured["max_retries"] == 7
    assert captured["web_tool_profile"] == "web_search.default"

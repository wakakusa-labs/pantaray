from __future__ import annotations

# pylint: disable=import-error,redefined-outer-name,unused-argument,protected-access
import pytest
from tests.unit.agents.action_agent.fixtures import base_state

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolRuntimeContext,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.validation import (
    validate_tool_args,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    _run_memory_search,
)
from pantaray_agents.agents.action_agent.tools import MEMORY_SEARCH_TOOL
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    build_memory_context_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MemorySearchRequest,
    MemorySearchResponse,
)
from pantaray_agents.schema.agent.base import StatusType


def test_memory_search_accepts_agent_experience_focus() -> None:
    validate_tool_args(
        MEMORY_SEARCH_TOOL,
        {"query": "reusable procedure", "focus": "agent_experience"},
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("tokyo_local_zone")
async def test_memory_search_populates_results(
    action_agent: ActionAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = base_state(action_agent)
    captured: list[MemorySearchRequest] = []

    async def execute_memory_search(**kwargs) -> MemorySearchResponse:
        request = kwargs["request"]
        assert isinstance(request, MemorySearchRequest)
        captured.append(request)
        return MemorySearchResponse(
            results=(
                {
                    "source": "long_term_insight",
                    "record_id": "ins-1",
                    "content": "長期インサイト",
                    "observed_at": "2026-09-26T21:50:00.000000Z",
                    "context_handle": "ctx-1",
                },
            ),
            epoch=build_memory_context_epoch(
                run_id=request.run_id,
                user_id=request.user_id,
                visible=(),
            ),
            semantic_status="available",
            notes=("Not matched by words: a.",),
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_search.execute_memory_search",
        execute_memory_search,
    )

    # Act
    result = await _run_memory_search(
        action_agent,
        step_id="step-ms",
        tool_def=MEMORY_SEARCH_TOOL,
        args={
            "query": "長期インサイト",
            "focus": "stable_knowledge",
            "time_hint": {
                "center": "2026-08-09T00:00:00Z",
                "radius_hours": 24,
            },
            "limit": 1,
        },
        state=state,
        tool_runtime_context=ToolRuntimeContext(max_parallel_memory_queries=4),
    )

    # Assert
    assert result.status == StatusType.SUCCESS.value
    assert result.output["results"], "検索結果が空です"
    assert result.output["results"][0]["observed_at"] == "2026-09-27T06:50+09:00"
    # Each result names its source, so the output carries no second copy by source.
    assert set(result.output) == {
        "results",
        "semantic_status",
        "semantic_error_code",
        "notes",
    }
    assert result.output["notes"] == [
        "Not matched by words: a.",
        "The results stop at limit=1; more may match. To reach others, pass a "
        "larger limit (up to 100) or search with more specific terms.",
    ]
    request = captured[0]
    assert request.user_id == state["user_id"]
    assert request.run_id == "step-ms"
    assert request.query == "長期インサイト"
    assert request.focus == "stable_knowledge"
    assert request.center_time == "2026-08-09T00:00:00Z"
    assert request.radius_hours == 24
    assert request.limit == 1
    assert request.pinned_revisions is None
    assert request.current_epoch is None

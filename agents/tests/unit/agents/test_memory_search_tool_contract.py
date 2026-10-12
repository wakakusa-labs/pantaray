from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolRuntimeContext,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    ToolValidationError,
    _run_memory_search,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import MEMORY_SEARCH_TOOL
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_epoch,
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.context_search import (
    merge_memory_search_context,
)
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    append_memory_context_item,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryContextEpoch,
    MemorySearchResult,
)
from pantaray_agents.local_runtime.memory_catalog.search_service import (
    MemorySearchRequest,
    MemorySearchResponse,
)
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.repositories.action_support.memory_search_helpers import (
    MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS,
)


@pytest.fixture()
def action_agent() -> ActionAgent:
    repo = MockActionAgentRepository()
    llm_client = MockLLMClient()
    return ActionAgent(
        config={
            "supabase_client": object(),
            "llm_client": llm_client,
            "llm": {},
        },
        repository=repo,
    )


def _base_state(action_agent: ActionAgent) -> dict:
    _ = action_agent
    return create_initial_state(
        user_id="user-123",
        suggestion_id="sug-123",
        action_id="act-123",
        started_at="2025-01-01T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )


def _tool_runtime_context() -> ToolRuntimeContext:
    return ToolRuntimeContext(max_parallel_memory_queries=4)


def test_memory_search_time_hint_schema_requires_complete_hint() -> None:
    schema = MEMORY_SEARCH_TOOL.build_validation_input_schema()
    properties = schema["properties"]
    assert isinstance(properties, dict)
    time_hint = properties["time_hint"]
    assert isinstance(time_hint, dict)
    assert time_hint["required"] == ["center", "radius_hours"]


@pytest.mark.asyncio
async def test_memory_search_merges_epoch_and_reuses_visible_handle(
    action_agent: ActionAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _base_state(action_agent)
    base, visible = append_memory_context_item(
        epoch=MemoryContextEpoch("epoch-base", "action-run", "user-123", ()),
        source="fact",
        label="visible",
        source_path="facts.md",
        heading_path=None,
        content="old visible content",
        fragment_id="fragment-shared",
        revision_id="revision-shared",
        node_id="node-shared",
        reference_depth=0,
    )
    added, duplicate = append_memory_context_item(
        epoch=MemoryContextEpoch("epoch-search", "action-run", "user-123", ()),
        source="fact",
        label="duplicate",
        source_path="facts.md",
        heading_path=None,
        content="new duplicate content",
        fragment_id="fragment-shared",
        revision_id="revision-shared",
        node_id="node-shared",
        reference_depth=0,
    )
    added, new_item = append_memory_context_item(
        epoch=added,
        source="long_term_insight",
        label="new",
        source_path="insights.md",
        heading_path=None,
        content="new content",
        fragment_id="fragment-new",
        revision_id="revision-new",
        node_id="node-new",
        reference_depth=0,
    )
    state["memory_context_epoch"] = serialize_memory_epoch(base)
    search_results: list[MemorySearchResult] = [
        {
            "source": "facts",
            "record_id": "shared",
            "content": "new duplicate content",
            "source_path": "facts.md",
            "heading_path": None,
            "observed_at": "2026-08-09T00:00:00Z",
            "matched_by": "keywords",
            "context_handle": duplicate.item.context_handle,
        },
        {
            "source": "long_term_insight",
            "record_id": "new",
            "content": "new content",
            "source_path": "insights.md",
            "heading_path": None,
            "observed_at": "2026-08-09T00:00:00Z",
            "matched_by": "keywords",
            "context_handle": new_item.item.context_handle,
        },
    ]
    merged_results, merged_epoch = merge_memory_search_context(
        results=search_results,
        current_epoch=base,
        search_epoch=added,
    )
    execute = AsyncMock(
        return_value=MemorySearchResponse(
            results=tuple(merged_results),
            epoch=merged_epoch,
            semantic_status="available",
        )
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_search.execute_memory_search",
        execute,
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    result = await _run_memory_search(
        action_agent,
        step_id="second-search-step",
        tool_def=MEMORY_SEARCH_TOOL,
        args={"query": "new"},
        state=state,
        tool_runtime_context=_tool_runtime_context(),
    )

    request = execute.await_args.kwargs["request"]
    assert isinstance(request, MemorySearchRequest)
    assert request.run_id == "action-run"
    assert request.current_epoch == base
    assert result.output["results"][0]["context_handle"] == (
        visible.item.context_handle
    )
    merged = deserialize_memory_epoch(state["memory_context_epoch"])
    assert [item.fragment_id for item in merged.items] == [
        "fragment-shared",
        "fragment-new",
    ]
    assert merged.items[0].item.context_handle == visible.item.context_handle


@pytest.mark.asyncio
async def test_memory_search_requires_query(action_agent: ActionAgent) -> None:
    state = _base_state(action_agent)

    with pytest.raises(ToolValidationError) as exc_info:
        await _run_memory_search(
            action_agent,
            step_id="step-ms-missing-query",
            tool_def=MEMORY_SEARCH_TOOL,
            args={},
            state=state,
            tool_runtime_context=_tool_runtime_context(),
        )

    assert exc_info.value.details is not None
    assert "query" in str(exc_info.value.details.get("message"))


@pytest.mark.asyncio
async def test_memory_search_passes_query_focus_and_time_hint(
    action_agent: ActionAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _base_state(action_agent)
    execute = AsyncMock(
        return_value=MemorySearchResponse(
            results=(),
            epoch=MemoryContextEpoch("epoch-1", "step-ms-contract", "user-123", ()),
            semantic_status="provider_unavailable",
            semantic_error_code="MEMORY_EMBEDDING_FAILED",
        )
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_search.execute_memory_search",
        execute,
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    time_hint = {"center": "2025-01-01T00:00:00Z", "radius_hours": 12}

    result = await _run_memory_search(
        action_agent,
        step_id="step-ms-contract",
        tool_def=MEMORY_SEARCH_TOOL,
        args={
            "query": "needle context",
            "focus": "activity",
            "time_hint": time_hint,
        },
        state=state,
        tool_runtime_context=_tool_runtime_context(),
    )

    execute.assert_awaited_once()
    request = execute.await_args.kwargs["request"]
    assert isinstance(request, MemorySearchRequest)
    assert request.query == "needle context"
    assert request.focus == "activity"
    assert request.center_time == time_hint["center"]
    assert request.radius_hours == time_hint["radius_hours"]
    assert result.status == "success"
    assert result.output["semantic_status"] == "provider_unavailable"
    assert result.output["semantic_error_code"] == "MEMORY_EMBEDDING_FAILED"


@pytest.mark.asyncio
async def test_memory_search_preserves_repository_relevance_order(
    action_agent: ActionAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _base_state(action_agent)
    rows: tuple[MemorySearchResult, ...] = (
        {
            "source": "action",
            "record_id": "old-high-score",
            "content": "older but more relevant",
            "source_path": "actions/old.md",
            "heading_path": None,
            "observed_at": "2024-01-01T00:00:00Z",
            "matched_by": "keywords",
        },
        {
            "source": "action",
            "record_id": "new-low-score",
            "content": "newer but less relevant",
            "source_path": "actions/new.md",
            "heading_path": None,
            "observed_at": "2026-01-01T00:00:00Z",
            "matched_by": "keywords",
        },
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_search.execute_memory_search",
        AsyncMock(
            return_value=MemorySearchResponse(
                results=rows,
                epoch=MemoryContextEpoch("epoch-2", "step-ms-order", "user-123", ()),
                semantic_status="available",
            )
        ),
    )
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")

    result = await _run_memory_search(
        action_agent,
        step_id="step-ms-order",
        tool_def=MEMORY_SEARCH_TOOL,
        args={"query": "needle"},
        state=state,
        tool_runtime_context=_tool_runtime_context(),
    )

    rows = result.output["results"]
    assert [row["record_id"] for row in rows] == [
        "old-high-score",
        "new-low-score",
    ]


@pytest.mark.asyncio
async def test_memory_search_rejects_invalid_time_hint(
    action_agent: ActionAgent,
) -> None:
    state = _base_state(action_agent)

    with pytest.raises(ToolValidationError) as exc_info:
        await _run_memory_search(
            action_agent,
            step_id="step-ms-invalid-time-hint",
            tool_def=MEMORY_SEARCH_TOOL,
            args={
                "query": "needle",
                "time_hint": {"center": "not-a-date", "radius_hours": 1},
            },
            state=state,
            tool_runtime_context=_tool_runtime_context(),
        )

    assert exc_info.value.details is not None
    assert exc_info.value.details.get("code") == "INVALID_MEMORY_SEARCH_TIME_HINT"
    assert exc_info.value.details.get("path") == ["time_hint", "center"]


@pytest.mark.asyncio
async def test_memory_search_rejects_timezone_naive_time_hint(
    action_agent: ActionAgent,
) -> None:
    state = _base_state(action_agent)

    with pytest.raises(ToolValidationError) as exc_info:
        await _run_memory_search(
            action_agent,
            step_id="step-ms-naive-time-hint",
            tool_def=MEMORY_SEARCH_TOOL,
            args={
                "query": "needle",
                "time_hint": {"center": "2025-01-01T00:00:00", "radius_hours": 1},
            },
            state=state,
            tool_runtime_context=_tool_runtime_context(),
        )

    assert exc_info.value.details is not None
    assert exc_info.value.details.get("code") == "INVALID_MEMORY_SEARCH_TIME_HINT"


@pytest.mark.asyncio
async def test_memory_search_rejects_partial_time_hint(
    action_agent: ActionAgent,
) -> None:
    state = _base_state(action_agent)

    with pytest.raises(ToolValidationError) as exc_info:
        await _run_memory_search(
            action_agent,
            step_id="step-ms-partial-time-hint",
            tool_def=MEMORY_SEARCH_TOOL,
            args={
                "query": "needle",
                "time_hint": {"center": "2025-01-01T00:00:00Z"},
            },
            state=state,
            tool_runtime_context=_tool_runtime_context(),
        )

    assert exc_info.value.details is not None
    assert exc_info.value.details.get("path") == ["time_hint"]
    assert "radius_hours" in str(exc_info.value.details.get("message"))


@pytest.mark.asyncio
async def test_memory_search_rejects_too_large_time_hint_radius(
    action_agent: ActionAgent,
) -> None:
    state = _base_state(action_agent)

    with pytest.raises(ToolValidationError) as exc_info:
        await _run_memory_search(
            action_agent,
            step_id="step-ms-large-time-hint",
            tool_def=MEMORY_SEARCH_TOOL,
            args={
                "query": "needle",
                "time_hint": {
                    "center": "2025-01-01T00:00:00Z",
                    "radius_hours": MEMORY_SEARCH_MAX_TIME_HINT_RADIUS_HOURS + 1,
                },
            },
            state=state,
            tool_runtime_context=_tool_runtime_context(),
        )

    assert exc_info.value.details is not None
    assert exc_info.value.details.get("path") == ["time_hint", "radius_hours"]

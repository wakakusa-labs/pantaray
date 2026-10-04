"""Returned tool failures agree across history, formal steps, and checkpoints."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.test_action_agent_tool_batch_execution import (
    _act,
    _build_fixture,
    _persisted_tool_steps,
    _think,
    _tool_history,
    _turn,
)

from pantaray_agents.agents.action_agent.runtime.handlers import web_search_runtime
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.utils.trace_context import TraceContextManager


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True], ids=["single", "parallel"])
async def test_missing_read_is_failed_in_history_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parallel: bool
) -> None:
    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-read-history",
        allowed_tool_ids=("read",),
    )
    existing = Path(state["action_temp_dir"]) / "existing.txt"
    existing.write_text("hello", encoding="utf-8")
    missing = existing.with_name("missing.txt")
    calls = [("read", {"path": str(missing)})]
    if parallel:
        calls.append(("read", {"path": str(existing)}))
    agent._generate_llm_action_turn = AsyncMock(return_value=_turn(*calls))
    state = await _think(agent, runtime, state)

    with TraceContextManager(extra={"process_id": "proc-read-history"}):
        state = await _act(agent, runtime, state)

    steps = _persisted_tool_steps(agent, request.action_id)
    failed = steps[0]
    assert failed["status"] == "error"
    assert "READ_PATH_NOT_FOUND" in failed["tool_output"]["output"]["error"]["message"]
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        statuses = connection.execute(
            "SELECT status FROM tool_invocations WHERE tool_request_id = ?",
            (failed["step_id"],),
        ).fetchall()
    assert statuses == [("failed",)]

    failed_entry = next(
        entry for entry in _tool_history(state) if entry["step_id"] == failed["step_id"]
    )
    expected = "read: failed ACTION_TOOL_READ_FAILED"
    assert failed_entry["result_line"] == expected
    # The conversation replays the call the model made; without its arguments
    # the model would read a read it never wrote.
    assert failed_entry["args"] == {"path": str(missing)}
    checkpoint = failed["runtime_state_checkpoint"]
    saved_entry = next(
        entry
        for entry in checkpoint["history_by_scope"]["S"]
        if entry["step_id"] == failed["step_id"]
    )
    assert saved_entry["result_line"] == expected
    formatter = ActionAgentFormatter()
    assert f"- Result: {expected}" in formatter.format_history(state)
    assert f"- Result: {expected}" in formatter.format_history(
        state, omit_before_step_number=state["step"]
    )
    if parallel:
        assert steps[1]["status"] == "success"
        assert steps[1]["tool_output"]["output"]["content"] == "hello"
        assert "- Result: read: ok, 5 chars" in formatter.format_history(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_size", "storage_kind", "expected_code"),
    [
        (10, "inline_json", "PROXY_INVALID_UPSTREAM_RESPONSE"),
        (21_000, "action_file", "ACTION_TOOL_WEB_SEARCH_FAILED"),
    ],
    ids=["inline", "spilled"],
)
async def test_non_broker_failure_history_uses_finalized_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    query_size: int,
    storage_kind: str,
    expected_code: str,
) -> None:
    agent, runtime, state, request = await _build_fixture(
        monkeypatch,
        tmp_path,
        action_id="act-provider-failure-history",
        allowed_tool_ids=("web_search",),
    )
    agent._generate_llm_action_turn = AsyncMock(
        return_value=_turn(("web_search", {"query": "x" * query_size}))
    )
    state = await _think(agent, runtime, state)
    # Simulate a transport failure at the network boundary. Error mapping,
    # audit, spill projection, ACT and history all use their real implementations.
    monkeypatch.setattr(
        web_search_runtime,
        "invoke_web_tools_wrapper",
        AsyncMock(side_effect=RuntimeError("upstream unavailable")),
    )
    state = await _act(agent, runtime, state)

    saved = _persisted_tool_steps(agent, request.action_id)[0]
    assert saved["status"] == "error"
    assert saved["tool_output"]["output_storage_kind"] == storage_kind
    assert (
        _tool_history(state)[0]["result_line"] == f"web_search: failed {expected_code}"
    )

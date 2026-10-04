"""上限到達時のウィンドウ組み直し（決定事項 7）とキャッシュ規約（決定事項 8）のテスト。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    project_request_user_step,
)
from tests.unit.agents.action_agent.native_tool_test_support import native_tool_turn

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm import (
    context_budget,
    turn_input,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tools import (
    _run_history_fetch,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.support.formatter import ActionAgentFormatter
from pantaray_agents.agents.action_agent.tools import HISTORY_FETCH_TOOL
from pantaray_agents.agents.core.mixins.llm_usage import LlmUsage
from pantaray_agents.mock.mock_action_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.tool_use import LlmToolDefinition

_FORMATTER = ActionAgentFormatter()
_PROMPT_TEMPLATE = "{action_history}"


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


# --- history fixtures ---------------------------------------------------------------


def _entry(index: int, suffix: str, **overrides: Any) -> dict[str, Any]:
    step_type = StepType.LLM_OUTPUT if suffix == "THINK" else StepType.TOOL_EXECUTION
    return {
        "step_id": f"{suffix}-{index}",
        "step_number": index,
        "phase": "executing",
        "step_type": step_type,
        "summary": f"note {index}",
        "tool_id": "read",
        "started_at": f"2026-09-08T00:00:{index % 60:02d}Z",
        "completed_at": f"2026-09-08T00:00:{index % 60:02d}Z",
        "short_step_id": f"S-{index}-{suffix}",
    } | overrides


def _think(index: int, **overrides: Any) -> dict[str, Any]:
    return _entry(index, "THINK", **overrides)


def _tool(index: int, **overrides: Any) -> dict[str, Any]:
    return (
        _entry(index, "TOOL")
        | {
            "result_line": f"read: ok, {index} chars",
            "args": {"path": f"/tmp/{index}"},
            "output": {"kind": "file", "content": f"body {index}"},
        }
        | overrides
    )


def _turns(count: int) -> list[dict[str, Any]]:
    return [e for i in range(1, count + 1) for e in (_think(i), _tool(i))]


def _formatter_state(entries: list[dict[str, Any]]) -> Any:
    return cast(Any, {"history_by_scope": {"S": entries}})


# --- 古い結果本文だけをバッチ単位で省略する ------------------------------------------


def test_pruning_preserves_metadata_and_does_not_mutate_history() -> None:
    entries = _turns(10)
    for entry in entries:
        if entry["step_type"] == StepType.TOOL_EXECUTION:
            entry["output"] = {"content": f"body {entry['step_number']} " + "x" * 2000}
    state = _formatter_state(entries)
    full = _FORMATTER.format_history(state)
    byte_budget = len(full.encode("utf-8")) // 2
    boundary = _FORMATTER.resolve_history_omission_boundary(
        state, byte_budget=byte_budget
    )
    pruned = _FORMATTER.format_history(state, omit_before_step_number=boundary)

    assert 0 < boundary <= 10
    assert len(pruned.encode("utf-8")) <= byte_budget
    for index in range(1, 11):
        assert f"- Note: note {index}" in pruned
        assert f"- History Ref: S-{index}-TOOL" in pruned
        assert f'"/tmp/{index}"' in pruned
        assert f"- Result: read: ok, {index} chars" in pruned
        assert (f'"body {index} ' in pruned) is (index >= boundary)
    assert _FORMATTER.format_history(state) == full
    assert (
        _FORMATTER.resolve_history_omission_boundary(
            state, byte_budget=10**7, omit_before_step_number=boundary
        )
        == boundary
    )


def test_body_is_not_pruned_when_it_fits_the_budget() -> None:
    state = _formatter_state(_turns(3))
    assert _FORMATTER.resolve_history_omission_boundary(state, byte_budget=10**7) == 0


def test_pruning_drops_whole_batches_and_keeps_latest_even_over_budget() -> None:
    entries = [_think(1), _tool(1), _tool(2), _think(3), _tool(3), _tool(4)]
    state = _formatter_state(entries)
    boundary = _FORMATTER.resolve_history_omission_boundary(state, byte_budget=0)
    text = _FORMATTER.format_history(state, omit_before_step_number=boundary)
    assert boundary == 3
    for index in (1, 2, 3, 4):
        assert f"S-{index}-TOOL" in text
        assert (f'"body {index}"' in text) is (index >= 3)
    # A repeated projection with a saved boundary never restores omitted outputs.
    assert (
        _FORMATTER.resolve_history_omission_boundary(
            state, byte_budget=0, omit_before_step_number=boundary
        )
        == boundary
    )


@pytest.mark.asyncio
async def test_history_fetch_still_resolves_a_ref_the_body_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文を省略したステップも History Ref で取り直せる（失敗シナリオ 8）。"""

    collapsed = _FORMATTER.format_history(
        _formatter_state(_turns(10)), scope_handle="S", omit_before_step_number=6
    )
    assert "S-2-TOOL" in collapsed
    assert '"body 2"' not in collapsed
    action_agent = _make_agent()

    async def fake_get_action_steps_by_short_step_ids(
        *, user_id: str, action_id: str, short_step_ids: tuple[str, ...]
    ) -> RepositoryResult[list[DBRow]]:
        assert short_step_ids == ("S-2-TOOL",)
        return RepositoryResult(
            data=[
                cast(
                    DBRow,
                    {
                        "short_step_id": "S-2-TOOL",
                        "step_number": 2,
                        "local_step_number": 2,
                        "step_name": "tool::read",
                        "step_type": "tool_execution",
                        "status": "success",
                        "user_request_text": None,
                        "thinking": None,
                        "llm_prompt_text": None,
                        "llm_response_text": None,
                        "tool_args": {"path": "/tmp/2"},
                        "tool_output": {"kind": "file", "content": "body 2"},
                        "error": None,
                        "goal_handle": "S",
                        "started_at": "2026-09-08T00:00:02Z",
                        "completed_at": "2026-09-08T00:00:02Z",
                    },
                )
            ]
        )

    monkeypatch.setattr(
        action_agent.repository,
        "get_action_steps_by_short_step_ids",
        fake_get_action_steps_by_short_step_ids,
    )
    from tests.unit.agents.action_agent.fixtures import base_state

    result = await _run_history_fetch(
        action_agent,
        step_id="internal-fetch-step",
        tool_def=HISTORY_FETCH_TOOL,
        args={"refs": ["S-2-TOOL"]},
        state=base_state(action_agent),
    )

    assert json.loads(result.output["content"])["steps"][0]["tool_output"] == {
        "kind": "file",
        "content": "body 2",
    }


# --- THINK 境界での組み直し ----------------------------------------------------------


def _make_agent() -> ActionAgent:
    repo = MockActionAgentRepository()

    def _fake_load_config(_prompt_name: str) -> PromptConfig:
        return PromptConfig(prompt=_PROMPT_TEMPLATE, system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=repo,
        )
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent._consume_llm_thoughts = MagicMock(return_value=None)  # type: ignore[attr-defined]
    return agent


async def _build_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, action_id: str, prior_turns: int = 4
):
    agent = _make_agent()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id=action_id,
        suggestion_id=f"sug-{action_id}",
        user_id=f"user-{action_id}",
        user_step_number=prior_turns + 1,
        user_step_local_step_number=prior_turns + 1,
    )
    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 20,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
        },
        emit_action_step=noop,
        emit_error=AsyncMock(),
        services=agent._runtime_services,  # noqa: SLF001
    )
    started_at = datetime.now(UTC).isoformat()
    state = create_initial_state(
        user_id=request.user_id,
        suggestion_id=request.suggestion_id,
        action_id=request.action_id,
        started_at=started_at,
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["history_by_scope"]["S"] = cast(Any, _turns(prior_turns))
    state["context"]["local_step_counters"] = {"S": prior_turns}
    state = project_request_user_step(state, request)
    install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=("read_action_plan",),
    )
    await agent.repository.save_action(
        {
            "action_id": request.action_id,
            "suggestion_id": request.suggestion_id,
            "user_id": request.user_id,
            "final_output": "",
            "status": "processing",
            "prompt_name": "action/executing",
            "prompt_version": "test",
            "created_at": started_at,
            "updated_at": started_at,
        },
        prompt_name="action/executing",
        prompt_version="test",
    )
    return agent, runtime, state


def _install_think(agent: ActionAgent, *, prompt_tokens: int, cached: int = 0) -> None:
    """provider が usage を返す THINK をモックする。"""

    async def _call(*, sink, **_kwargs):  # type: ignore[no-untyped-def]
        sink.record(
            LlmUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=32,
                fields={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 32,
                    "cached_prompt_tokens": cached,
                },
            ),
            stage="executing",
            may_raise=True,
        )
        return native_tool_turn("read_action_plan", {})

    agent._generate_llm_action_turn = AsyncMock(side_effect=_call)  # type: ignore[attr-defined]


def _think_entries(state: Any) -> list[dict[str, Any]]:
    return [
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.LLM_OUTPUT
    ]


@pytest.mark.asyncio
async def test_reaching_85_percent_rebuilds_at_the_next_think_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """85% 到達では予約だけし、次の THINK 境界で組み直す（失敗シナリオ 6）。"""

    agent, runtime, state = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-reset-85"
    )
    # The real tool definitions (about 55 KB) are part of the fixed input, so the
    # window leaves room for them plus one body inside the 50% target.
    monkeypatch.setattr(context_budget, "_window_tokens", lambda: 40_000)
    for entry in state["history_by_scope"]["S"]:
        if entry["step_type"] == StepType.TOOL_EXECUTION:
            entry["output"] = f"body {entry['step_number']} " + "x" * 16_000
    _install_think(agent, prompt_tokens=34_400)

    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    assert state["context"]["context_reset_pending"] is True
    armed_prompt = state["context"]["last_supervisor_prompt"]
    assert context_budget.CONTEXT_RESET_RESULT_LINE not in str(
        _think_entries(state)[-1].get("result_line") or ""
    )
    # 予約したターン自体は本文を省略しない。
    assert '"body 1 ' in armed_prompt

    _install_think(agent, prompt_tokens=14_000)
    state["step"] = 11
    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    rebuilt_prompt = state["context"]["last_supervisor_prompt"]
    for index in range(1, 5):
        assert f"- Note: note {index}" in rebuilt_prompt
        assert f"- History Ref: S-{index}-TOOL (use history_fetch)" in rebuilt_prompt
        assert (f'"body {index} ' in rebuilt_prompt) is (index == 4)
    assert state["context"]["context_input_baseline"]["rendered_bytes"] <= 20_000 * 4
    assert state["context"]["context_reset_pending"] is False
    assert _think_entries(state)[-1]["result_line"] == (
        context_budget.CONTEXT_RESET_RESULT_LINE
    )


@pytest.mark.asyncio
async def test_passing_95_percent_rebuilds_immediately_without_being_armed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """95% 超は予約を待たずその THINK の前に組み直す（失敗シナリオ 6）。"""

    agent, runtime, state = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-reset-95", prior_turns=6
    )
    monkeypatch.setattr(context_budget, "_window_tokens", lambda: 30_000)
    state["context"]["context_reset_pending"] = False
    # The first request has no usage baseline. Its full input exceeds 95%.
    for entry in state["history_by_scope"]["S"]:
        if entry["step_type"] == StepType.TOOL_EXECUTION:
            entry["output"] = f"body {entry['step_number']} " + "x" * 16_000
    _install_think(agent, prompt_tokens=1_000)

    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]

    prompt = state["context"]["last_supervisor_prompt"]
    assert '"body 6 ' in prompt
    assert '"body 1 ' not in prompt
    assert "S-4-TOOL" in prompt
    assert _think_entries(state)[-1]["result_line"] == (
        context_budget.CONTEXT_RESET_RESULT_LINE
    )


@pytest.mark.asyncio
async def test_body_text_is_byte_identical_across_two_non_reset_thinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """リセットが起きない限り本体は書き換わらない（失敗シナリオ 3 の THINK 経路）。"""

    agent, runtime, state = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-reset-stable"
    )
    _install_think(agent, prompt_tokens=1_000, cached=900)

    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    first_body = state["context"]["last_supervisor_prompt"]

    state["step"] = 11
    state = await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )  # type: ignore[arg-type]
    second_body = state["context"]["last_supervisor_prompt"]

    assert second_body.encode("utf-8") == first_body.encode("utf-8")
    assert context_budget.stored_body_omission(state["context"]) == 0
    assert state["context"]["context_reset_pending"] is False


# --- キャッシュミス計測（決定事項 8） -------------------------------------------------


def _context(**values: Any) -> Any:
    return cast(Any, dict(values))


def _usage(prompt_tokens: int, *, cached: int) -> LlmUsage:
    return LlmUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        fields={"prompt_tokens": prompt_tokens, "cached_prompt_tokens": cached},
    )


def _record(
    context: Any, *, prompt_tokens: int, cached: int, did_rebuild: bool = False
) -> None:
    context_budget.record_think_usage(
        context,
        usage_before=LlmUsage(None, None),
        usage_after=_usage(prompt_tokens, cached=cached),
        rendered_bytes=1_000,
        did_rebuild=did_rebuild,
    )


def test_a_cache_miss_outside_a_reset_boundary_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context()
    _record(context, prompt_tokens=10_000, cached=10_000)

    with caplog.at_level(logging.INFO, logger=context_budget.logger.name):
        _record(context, prompt_tokens=12_000, cached=4_000)

    assert "cache_misses=6000" in caplog.text


def test_a_fully_cached_turn_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    context = _context()
    _record(context, prompt_tokens=10_000, cached=10_000)

    with caplog.at_level(logging.INFO, logger=context_budget.logger.name):
        _record(context, prompt_tokens=12_000, cached=10_000)

    assert caplog.text == ""


def test_the_first_think_after_a_reset_resets_the_cache_baseline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context()
    _record(context, prompt_tokens=200_000, cached=190_000)

    with caplog.at_level(logging.INFO, logger=context_budget.logger.name):
        _record(context, prompt_tokens=20_000, cached=0, did_rebuild=True)
    assert caplog.text == ""
    assert context["context_cache_prev_prompt_tokens"] == 20_000

    with caplog.at_level(logging.INFO, logger=context_budget.logger.name):
        _record(context, prompt_tokens=21_000, cached=20_000)
    assert caplog.text == ""


def test_a_think_without_provider_usage_keeps_the_previous_baseline() -> None:
    context = _context()
    _record(context, prompt_tokens=10_000, cached=0)

    context_budget.record_think_usage(
        context,
        usage_before=_usage(10_000, cached=0),
        usage_after=_usage(10_000, cached=0),
        rendered_bytes=999_999,
        did_rebuild=False,
    )

    assert context["context_input_baseline"] == {
        "prompt_tokens": 10_000,
        "rendered_bytes": 1_000,
    }
    assert context["context_reset_pending"] is True


def test_the_body_omission_boundary_never_moves_backwards() -> None:
    context = _context()

    assert context_budget.store_body_omission(context, boundary=12) == 12
    assert context_budget.store_body_omission(context, boundary=3) == 12


# --- 既存 checkpoint の互換 -----------------------------------------------------------


def test_a_checkpoint_without_the_new_bookkeeping_fields_restores_unchanged() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-09-08T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    legacy_context = cast("dict[str, Any]", checkpoint["context"])
    assert not [key for key in legacy_context if key.startswith("context_")]

    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert RUNTIME_STATE_CHECKPOINT_VERSION == 4
    assert restored["context"] == state["context"]
    assert context_budget.stored_body_omission(restored["context"]) == 0
    assert (
        context_budget.must_rebuild_window(restored["context"], rendered_bytes=10**7)
        is True
    )


def test_checkpoint_restores_the_same_pruned_prompt_and_raw_results() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-09-08T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["history_by_scope"]["S"] = cast(Any, _turns(4))
    context_budget.store_body_omission(state["context"], boundary=3)
    before = _FORMATTER.format_history(state, omit_before_step_number=3)
    _record(state["context"], prompt_tokens=4000, cached=0)
    restored = restore_runtime_state_checkpoint(
        build_runtime_state_checkpoint(state),
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )
    boundary = context_budget.stored_body_omission(restored["context"])
    assert context_budget.estimate_input_tokens(
        restored["context"], rendered_bytes=2000
    ) == context_budget.estimate_input_tokens(state["context"], rendered_bytes=2000)
    assert (
        _FORMATTER.format_history(restored, omit_before_step_number=boundary) == before
    )
    assert '"body 1"' in _FORMATTER.format_history(restored)
    restored["history_by_scope"]["S"].extend(cast(Any, [_think(5), _tool(5)]))
    after = _FORMATTER.format_history(restored, omit_before_step_number=boundary)
    assert after.startswith(before)
    assert '"body 1"' not in after
    assert '"body 5"' in after


@pytest.mark.parametrize("source", ["system", "memory", "tools", "repair"])
def test_complete_input_is_counted_and_protected_parts_can_exceed_target(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setattr(context_budget, "_window_tokens", lambda: 30_000)
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-09-08T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    entries = _turns(4)
    for entry in entries:
        if entry["step_type"] == StepType.TOOL_EXECUTION:
            entry["output"] = f"body {entry['step_number']} " + "x" * 16_000
    state["history_by_scope"]["S"] = cast(Any, entries)
    fixed = "固" * 21_334  # 64,002 UTF-8 bytes; 16,001 estimated tokens.
    system = fixed if source == "system" else ""
    memory = fixed if source == "memory" else ""
    notice = fixed if source == "repair" else ""
    tools = (
        [
            LlmToolDefinition(
                name="test",
                description=fixed,
                parameters={"type": "object"},
            )
        ]
        if source == "tools"
        else []
    )
    prepared = turn_input.ExecutingTurn(
        head=memory,
        system_instruction=system,
        tool_bytes=sum(len(tool.model_dump_json().encode("utf-8")) for tool in tools),
        scope_handles=("S",),
        sends_conversation=False,
    ).prepare(
        state,
        rendering=_make_agent()._runtime_services.rendering,
        repair_notice=notice,
        provider_turns={},
    )
    assert prepared.did_rebuild
    assert '"body 1 ' not in prepared.prompt
    assert '"body 4 ' in prepared.prompt
    assert 15_000 * 4 < prepared.rendered_bytes < 25_500 * 4
    assert fixed in prepared.prompt + system + "".join(t.description for t in tools)


@pytest.mark.parametrize(
    "rendered_bytes,expected", [(4000, 2000), (2000, 1500), (8000, 3000)]
)
def test_usage_calibration_accounts_for_input_growth_and_pruning(
    rendered_bytes: int,
    expected: int,
) -> None:
    context = _context(
        context_input_baseline={"prompt_tokens": 2000, "rendered_bytes": 4000}
    )
    assert (
        context_budget.estimate_input_tokens(context, rendered_bytes=rendered_bytes)
        == expected
    )


@pytest.mark.asyncio
async def test_oversized_protected_input_stops_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    agent, runtime, state = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-capacity"
    )
    monkeypatch.setattr(context_budget, "_window_tokens", lambda: 30_000)
    # User instructions are a real protected input and cannot be discarded.
    state["history_by_scope"]["S"][-1]["user_request_text"] = "x" * 120_000
    _install_think(agent, prompt_tokens=1_000)
    await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )
    agent._generate_llm_action_turn.assert_not_awaited()
    assert state["status"] == "error"
    assert state["next_action"] is None
    assert state["errors"][-1]["error_code"] == "ACTION_CONTEXT_CAPACITY_EXCEEDED"
    assert len(state["history_by_scope"]["S"][-1]["user_request_text"]) == 120_000


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.asyncio
async def test_repair_attempt_uses_latest_usage_and_records_the_sent_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    blocked: bool,
) -> None:
    agent, runtime, state = await _build_fixture(
        monkeypatch, tmp_path, action_id="act-repair-window"
    )
    # Sized like the 85% test: the real tool definitions are part of the input.
    monkeypatch.setattr(context_budget, "_window_tokens", lambda: 40_000)
    for entry in state["history_by_scope"]["S"]:
        if entry["step_type"] == StepType.TOOL_EXECUTION:
            entry["output"] = f"body {entry['step_number']} " + "x" * 16_000
    prompts: list[str] = []

    async def call(*, sink, prompt, **kwargs):  # type: ignore[no-untyped-def]
        prompts.append(prompt)
        sink.record(
            LlmUsage(
                prompt_tokens=(53_400 if blocked else 34_400)
                if len(prompts) == 1
                else 14_000,
                completion_tokens=32,
            ),
            stage="executing",
            may_raise=True,
        )
        return native_tool_turn(
            "read_action_plan",
            {},
            step_note=None if len(prompts) == 1 else "Read the plan.",
        )

    agent._generate_llm_action_turn = AsyncMock(side_effect=call)
    await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )
    assert len(prompts) == (1 if blocked else 2)
    assert '"body 1 ' in prompts[0]
    if blocked:
        assert state["status"] == "error"
        assert state["errors"][-1]["error_code"] == "ACTION_CONTEXT_CAPACITY_EXCEEDED"
        assert state["next_action"] is None
    else:
        assert '"body 1 ' not in prompts[1]
        assert "# System Notice" in prompts[1]
        assert '"body 4 ' in prompts[1]
        assert state["context"]["context_input_baseline"]["prompt_tokens"] == 14_000
    assert state["context"]["last_supervisor_prompt"] == prompts[-1]
    rows = cast(MockActionAgentRepository, agent.repository).data["action_steps"]
    assert rows[-1]["llm_prompt_text"] == prompts[-1]


@pytest.mark.parametrize("output", [None, False, 0, "", [], {}])
def test_pruning_small_valid_results_never_increases_input(output: object) -> None:
    state = _formatter_state([_think(1), _tool(1, output=output), _think(2), _tool(2)])
    before = _FORMATTER.format_history(state)
    boundary = _FORMATTER.resolve_history_omission_boundary(state, byte_budget=0)
    after = _FORMATTER.format_history(state, omit_before_step_number=boundary)
    assert len(after.encode("utf-8")) < len(before.encode("utf-8"))
    assert "S-1-TOOL" in after
    assert '"body 2"' in after

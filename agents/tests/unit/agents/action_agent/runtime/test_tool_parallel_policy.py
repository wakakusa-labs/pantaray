from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Sequence

import pytest

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.broker_tools import (
    run_broker_tool_wrapper,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.external_tools import (
    run_history_fetch_wrapper,
    run_web_crawl_wrapper,
    run_web_extract_wrapper,
    run_web_search_wrapper,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.memory_links import (
    run_get_memory_reference_tool,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.memory_search import (
    run_memory_search_tool,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.memory_sql import (
    run_memory_sql_tool,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.parallel_policy import (
    MEMORY_EPOCH_WRITER_TOOL_IDS,
    PARALLEL_SAFE_TOOL_IDS,
    SERIAL_ONLY_TOOL_IDS,
    SOLO_TURN_TOOL_IDS,
    ExcludedToolCall,
    plan_tool_batch,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.plan_document import (
    run_action_plan_tool,
)
from pantaray_agents.agents.action_agent.runtime.models.tool_call import ToolCallModel
from pantaray_agents.agents.action_agent.tools import (
    SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
)

# run_validated_tool_impl / run_tool の dispatch を写した、allowlist ツールの実装関数。
# 新しい allowlist ツールを増やすときはここにも実装関数を登録する。
PARALLEL_SAFE_HANDLERS: dict[str, Callable[..., object]] = {
    "read": run_broker_tool_wrapper,
    "list": run_broker_tool_wrapper,
    "glob": run_broker_tool_wrapper,
    "grep": run_broker_tool_wrapper,
    "web_search": run_web_search_wrapper,
    "web_extract": run_web_extract_wrapper,
    "web_crawl": run_web_crawl_wrapper,
    "history_fetch": run_history_fetch_wrapper,
    "memory_search": run_memory_search_tool,
    "memory_sql": run_memory_sql_tool,
    "get_memory_reference": run_get_memory_reference_tool,
    "read_action_plan": run_action_plan_tool,
}


def _call(tool_id: str) -> ToolCallModel:
    return ToolCallModel(tool_id=tool_id, args={})


def _tool_ids(calls: Sequence[ToolCallModel]) -> list[str]:
    return [call.tool_id for call in calls]


def _excluded(
    entries: Sequence[ExcludedToolCall[ToolCallModel]],
) -> list[tuple[str, str]]:
    return [(entry.call.tool_id, entry.reason) for entry in entries]


def _state_write_keys(handler: Callable[..., object]) -> set[str]:
    """``state[...] = ...`` / ``state.pop(...)`` 等で書き換えられるキーを集める。"""

    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    keys: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "state"
            ):
                keys.add(ast.unparse(target.slice))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "state"
            and node.func.attr in {"pop", "popitem", "setdefault", "update", "clear"}
        ):
            keys.add(f"state.{node.func.attr}(...)")
    return keys


def test_every_supervisor_tool_id_has_exactly_one_classification() -> None:
    # A subagent's tools are the Supervisor's, plus its own terminal report.
    tool_ids = {*SUPERVISOR_SINGLE_REACT_TOOL_IDS, SUBMIT_SUBAGENT_REPORT_TOOL_ID}
    classified = PARALLEL_SAFE_TOOL_IDS | SOLO_TURN_TOOL_IDS | SERIAL_ONLY_TOOL_IDS

    assert classified == tool_ids
    assert not PARALLEL_SAFE_TOOL_IDS & SOLO_TURN_TOOL_IDS
    assert not PARALLEL_SAFE_TOOL_IDS & SERIAL_ONLY_TOOL_IDS
    assert not SOLO_TURN_TOOL_IDS & SERIAL_ONLY_TOOL_IDS
    assert MEMORY_EPOCH_WRITER_TOOL_IDS <= PARALLEL_SAFE_TOOL_IDS


def test_parallel_safe_handlers_are_registered_for_the_safety_checks() -> None:
    assert set(PARALLEL_SAFE_HANDLERS) == PARALLEL_SAFE_TOOL_IDS


@pytest.mark.parametrize("tool_id", sorted(PARALLEL_SAFE_TOOL_IDS))
def test_parallel_safe_handler_does_not_take_a_token_sink(tool_id: str) -> None:
    parameters = inspect.signature(PARALLEL_SAFE_HANDLERS[tool_id]).parameters

    assert "sink" not in parameters


@pytest.mark.parametrize("tool_id", sorted(PARALLEL_SAFE_TOOL_IDS))
def test_parallel_safe_handler_only_writes_the_memory_epoch_state_key(
    tool_id: str,
) -> None:
    expected = (
        {"'memory_context_epoch'"} if tool_id in MEMORY_EPOCH_WRITER_TOOL_IDS else set()
    )

    assert _state_write_keys(PARALLEL_SAFE_HANDLERS[tool_id]) == expected


def test_all_parallel_safe_calls_run_in_parallel() -> None:
    plan = plan_tool_batch(
        [_call("read"), _call("grep"), _call("web_search")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "grep", "web_search"]
    assert plan.mode == "parallel"
    assert plan.deferred == ()
    assert plan.dropped == ()


def test_serial_only_call_forces_the_whole_batch_sequential() -> None:
    plan = plan_tool_batch(
        [_call("read"), _call("apply_patch"), _call("grep")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "apply_patch", "grep"]
    assert plan.mode == "sequential"


def test_two_memory_searches_run_sequentially() -> None:
    plan = plan_tool_batch(
        [_call("memory_search"), _call("memory_search")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert len(plan.calls) == 2
    assert plan.mode == "sequential"


def test_memory_search_and_get_memory_reference_run_sequentially() -> None:
    plan = plan_tool_batch(
        [_call("memory_search"), _call("get_memory_reference")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert plan.mode == "sequential"


def test_single_memory_epoch_writer_still_runs_in_parallel() -> None:
    plan = plan_tool_batch(
        [_call("memory_search"), _call("read")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert plan.mode == "parallel"


def test_single_call_batch_uses_the_sequential_path() -> None:
    plan = plan_tool_batch([_call("read")], max_parallel=3, remaining_tool_steps=10)

    assert _tool_ids(plan.calls) == ["read"]
    assert plan.mode == "sequential"


def test_leading_solo_turn_tool_runs_alone_and_defers_the_rest() -> None:
    plan = plan_tool_batch(
        [_call("wait_subagents"), _call("read"), _call("grep")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["wait_subagents"]
    assert plan.mode == "sequential"
    assert _excluded(plan.deferred) == [
        ("read", "after_solo_turn_tool"),
        ("grep", "after_solo_turn_tool"),
    ]
    assert plan.dropped == ()


def test_trailing_solo_turn_tool_is_deferred_after_its_predecessors_run() -> None:
    plan = plan_tool_batch(
        [_call("read"), _call("wait_subagents"), _call("grep")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read"]
    assert _excluded(plan.deferred) == [
        ("wait_subagents", "solo_turn_tool"),
        ("grep", "after_solo_turn_tool"),
    ]


@pytest.mark.parametrize("tool_id", ("submit_final_answer", "submit_subagent_report"))
@pytest.mark.parametrize("position", ("first", "last"))
def test_a_run_ending_tool_shares_no_turn_and_the_others_still_run(
    tool_id: str, position: str
) -> None:
    # Run alone first, it would end the run before the deferred calls came back.
    others = [_call("read"), _call("grep")]
    calls = (
        [_call(tool_id), *others] if position == "first" else [*others, _call(tool_id)]
    )

    plan = plan_tool_batch(calls, max_parallel=3, remaining_tool_steps=10)

    assert _tool_ids(plan.calls) == ["read", "grep"]
    assert plan.mode == "parallel"
    assert _excluded(plan.deferred) == [(tool_id, "run_ending_tool")]


@pytest.mark.parametrize("tool_id", ("submit_final_answer", "submit_subagent_report"))
def test_two_run_ending_calls_in_one_turn_both_wait_and_nothing_runs(
    tool_id: str,
) -> None:
    # Letting the first through would end the run on it and lose the second.
    plan = plan_tool_batch(
        [_call(tool_id), _call(tool_id)], max_parallel=3, remaining_tool_steps=10
    )

    assert plan.calls == ()
    assert _excluded(plan.deferred) == [
        (tool_id, "run_ending_tool"),
        (tool_id, "run_ending_tool"),
    ]


def test_a_run_ending_tool_alone_runs() -> None:
    plan = plan_tool_batch(
        [_call("submit_final_answer")], max_parallel=3, remaining_tool_steps=10
    )

    assert _tool_ids(plan.calls) == ["submit_final_answer"]
    assert plan.deferred == ()


def test_spawn_subagents_run_sequentially_after_a_regular_tool() -> None:
    plan = plan_tool_batch(
        [_call("read"), _call("spawn_subagent"), _call("spawn_subagent")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "spawn_subagent", "spawn_subagent"]
    assert plan.mode == "sequential"
    assert plan.deferred == ()


def test_max_parallel_truncates_the_batch_in_declaration_order() -> None:
    plan = plan_tool_batch(
        [_call("read"), _call("grep"), _call("glob")],
        max_parallel=2,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "grep"]
    assert _excluded(plan.dropped) == [("glob", "max_parallel_exceeded")]


def test_remaining_tool_steps_truncates_before_max_parallel() -> None:
    plan = plan_tool_batch(
        [_call("read"), _call("grep"), _call("glob")],
        max_parallel=3,
        remaining_tool_steps=1,
    )

    assert _tool_ids(plan.calls) == ["read"]
    assert _excluded(plan.dropped) == [
        ("grep", "tool_step_budget_exhausted"),
        ("glob", "tool_step_budget_exhausted"),
    ]


def test_exhausted_tool_step_budget_drops_every_call() -> None:
    plan = plan_tool_batch([_call("read")], max_parallel=3, remaining_tool_steps=0)

    assert plan.calls == ()
    assert plan.mode == "sequential"
    assert _excluded(plan.dropped) == [("read", "tool_step_budget_exhausted")]


def test_empty_batch_plans_nothing() -> None:
    plan = plan_tool_batch([], max_parallel=3, remaining_tool_steps=10)

    assert plan.calls == ()
    assert plan.mode == "sequential"
    assert plan.deferred == ()
    assert plan.dropped == ()

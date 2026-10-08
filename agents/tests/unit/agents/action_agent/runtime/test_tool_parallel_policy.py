from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

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
from pantaray_agents.agents.action_agent.runtime.models.tool_call import ToolCallModel
from pantaray_agents.agents.action_agent.tools import (
    SUBMIT_SUBAGENT_REPORT_TOOL_ID,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_CONCURRENCY,
)
from pantaray_agents.conversation.tool_batch import (
    ExcludedToolCall,
    ToolBatchPlan,
    plan_tool_batch,
)
from pantaray_agents.local_runtime.runtime.action_subagent_broker_authority import (
    ActionSubagentBrokerAuthority,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    ActionSubagentJobPayload,
)
from pantaray_agents.tasks.internal_jobs.action_subagent_broker import (
    build_action_subagent_broker_tools,
)
from pantaray_agents.tasks.internal_jobs.action_subagent_history import (
    AgentsMdClaims,
)
from pantaray_agents.tools.contract import ToolConcurrency, ToolTurnPlacement

_EPOCH = "memory_context_epoch"

# Each tool's placement as the tool ID sets assigned it before tools declared
# it themselves; a change here changes which calls run at once or wait.
EXPECTED_CONCURRENCY: dict[str, ToolConcurrency] = {
    **{
        tool_id: ToolConcurrency("parallel")
        for tool_id in (
            "read",
            "list",
            "glob",
            "grep",
            "web_search",
            "web_extract",
            "web_crawl",
            "memory_sql",
            "history_fetch",
        )
    },
    "memory_search": ToolConcurrency("parallel", shared_state=_EPOCH),
    "get_memory_reference": ToolConcurrency("parallel", shared_state=_EPOCH),
    "wait_subagents": ToolConcurrency("solo_turn"),
    "submit_final_answer": ToolConcurrency("run_ending"),
    "submit_subagent_report": ToolConcurrency("run_ending"),
    **{
        tool_id: ToolConcurrency("sequential")
        for tool_id in (
            "thinking",
            "link_memory",
            "unlink_memory",
            "remember",
            "apply_patch",
            "bash",
            "run_python",
            "capture_screen",
            "render_pdf_page",
            "write_session_memory",
            "spawn_subagent",
            "send_message_to_subagent",
            "cancel_subagent",
            "draft_final_answer",
            "zanei_query",
            "zanei_timeline",
        )
    },
}

# What the subagent plans with: its broker tools plus its terminal report.
SUBAGENT_CONCURRENCY: dict[str, ToolConcurrency] = {
    **{
        tool.name: tool.concurrency
        for tool in build_action_subagent_broker_tools(
            db_path=Path("unused.sqlite3"),
            busy_timeout_ms=0,
            payload=cast(ActionSubagentJobPayload, {}),
            authority=cast(ActionSubagentBrokerAuthority, None),
            agents_md=AgentsMdClaims(attached=[]),
        )
    },
    SUBMIT_SUBAGENT_REPORT_TOOL_ID: ToolConcurrency("run_ending"),
}


def _tool_ids_declaring(placement: ToolTurnPlacement) -> list[str]:
    return sorted(
        tool_id
        for tool_id, declared in TOOL_CONCURRENCY.items()
        if declared.placement == placement
    )


PARALLEL_TOOL_IDS = _tool_ids_declaring("parallel")

# run_validated_tool_impl / run_tool の dispatch を写した、parallel ツールの実装関数。
# 新しく parallel を宣言するツールはここにも実装関数を登録する。
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
}


def _call(tool_id: str) -> ToolCallModel:
    return ToolCallModel(tool_id=tool_id, args={})


def _plan(
    calls: Sequence[ToolCallModel], *, max_parallel: int, remaining_tool_steps: int
) -> ToolBatchPlan[ToolCallModel]:
    # Action tools and the subagent's report together, as the two callers see them.
    return plan_tool_batch(
        calls,
        concurrency={**TOOL_CONCURRENCY, **SUBAGENT_CONCURRENCY},
        max_parallel=max_parallel,
        remaining_tool_steps=remaining_tool_steps,
    )


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


def test_every_action_tool_declares_its_former_placement() -> None:
    assert set(TOOL_CONCURRENCY) == set(SUPERVISOR_SINGLE_REACT_TOOL_IDS)
    assert dict(TOOL_CONCURRENCY) == {
        tool_id: EXPECTED_CONCURRENCY[tool_id] for tool_id in TOOL_CONCURRENCY
    }


def test_every_subagent_tool_declares_its_former_placement() -> None:
    assert SUBAGENT_CONCURRENCY == {
        tool_id: EXPECTED_CONCURRENCY[tool_id] for tool_id in SUBAGENT_CONCURRENCY
    }


def test_parallel_handlers_are_registered_for_the_safety_checks() -> None:
    assert sorted(PARALLEL_SAFE_HANDLERS) == PARALLEL_TOOL_IDS


@pytest.mark.parametrize("tool_id", PARALLEL_TOOL_IDS)
def test_parallel_handler_does_not_take_a_token_sink(tool_id: str) -> None:
    parameters = inspect.signature(PARALLEL_SAFE_HANDLERS[tool_id]).parameters

    assert "sink" not in parameters


@pytest.mark.parametrize("tool_id", PARALLEL_TOOL_IDS)
def test_parallel_handler_writes_only_the_state_it_declares(tool_id: str) -> None:
    shared_state = TOOL_CONCURRENCY[tool_id].shared_state
    expected = set() if shared_state is None else {repr(shared_state)}

    assert _state_write_keys(PARALLEL_SAFE_HANDLERS[tool_id]) == expected


def test_all_parallel_safe_calls_run_in_parallel() -> None:
    plan = _plan(
        [_call("read"), _call("grep"), _call("web_search")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "grep", "web_search"]
    assert plan.mode == "parallel"
    assert plan.deferred == ()
    assert plan.dropped == ()


def test_serial_only_call_forces_the_whole_batch_sequential() -> None:
    plan = _plan(
        [_call("read"), _call("apply_patch"), _call("grep")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "apply_patch", "grep"]
    assert plan.mode == "sequential"


def test_two_memory_searches_run_sequentially() -> None:
    plan = _plan(
        [_call("memory_search"), _call("memory_search")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert len(plan.calls) == 2
    assert plan.mode == "sequential"


def test_memory_search_and_get_memory_reference_run_sequentially() -> None:
    plan = _plan(
        [_call("memory_search"), _call("get_memory_reference")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert plan.mode == "sequential"


def test_single_memory_epoch_writer_still_runs_in_parallel() -> None:
    plan = _plan(
        [_call("memory_search"), _call("read")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert plan.mode == "parallel"


def test_single_call_batch_uses_the_sequential_path() -> None:
    plan = _plan([_call("read")], max_parallel=3, remaining_tool_steps=10)

    assert _tool_ids(plan.calls) == ["read"]
    assert plan.mode == "sequential"


def test_leading_solo_turn_tool_runs_alone_and_defers_the_rest() -> None:
    plan = _plan(
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
    plan = _plan(
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

    plan = _plan(calls, max_parallel=3, remaining_tool_steps=10)

    assert _tool_ids(plan.calls) == ["read", "grep"]
    assert plan.mode == "parallel"
    assert _excluded(plan.deferred) == [(tool_id, "run_ending_tool")]


@pytest.mark.parametrize("tool_id", ("submit_final_answer", "submit_subagent_report"))
def test_two_run_ending_calls_in_one_turn_both_wait_and_nothing_runs(
    tool_id: str,
) -> None:
    # Letting the first through would end the run on it and lose the second.
    plan = _plan(
        [_call(tool_id), _call(tool_id)], max_parallel=3, remaining_tool_steps=10
    )

    assert plan.calls == ()
    assert _excluded(plan.deferred) == [
        (tool_id, "run_ending_tool"),
        (tool_id, "run_ending_tool"),
    ]


def test_a_run_ending_tool_alone_runs() -> None:
    plan = _plan(
        [_call("submit_final_answer")], max_parallel=3, remaining_tool_steps=10
    )

    assert _tool_ids(plan.calls) == ["submit_final_answer"]
    assert plan.deferred == ()


def test_spawn_subagents_run_sequentially_after_a_regular_tool() -> None:
    plan = _plan(
        [_call("read"), _call("spawn_subagent"), _call("spawn_subagent")],
        max_parallel=3,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "spawn_subagent", "spawn_subagent"]
    assert plan.mode == "sequential"
    assert plan.deferred == ()


def test_max_parallel_truncates_the_batch_in_declaration_order() -> None:
    plan = _plan(
        [_call("read"), _call("grep"), _call("glob")],
        max_parallel=2,
        remaining_tool_steps=10,
    )

    assert _tool_ids(plan.calls) == ["read", "grep"]
    assert _excluded(plan.dropped) == [("glob", "max_parallel_exceeded")]


def test_remaining_tool_steps_truncates_before_max_parallel() -> None:
    plan = _plan(
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
    plan = _plan([_call("read")], max_parallel=3, remaining_tool_steps=0)

    assert plan.calls == ()
    assert plan.mode == "sequential"
    assert _excluded(plan.dropped) == [("read", "tool_step_budget_exhausted")]


def test_empty_batch_plans_nothing() -> None:
    plan = _plan([], max_parallel=3, remaining_tool_steps=10)

    assert plan.calls == ()
    assert plan.mode == "sequential"
    assert plan.deferred == ()
    assert plan.dropped == ()


def test_an_undeclared_tool_runs_in_order() -> None:
    # The registry answers a name no tool declared; it must not run at once.
    plan = _plan(
        [_call("read"), _call("not_offered")], max_parallel=3, remaining_tool_steps=10
    )

    assert _tool_ids(plan.calls) == ["read", "not_offered"]
    assert plan.mode == "sequential"

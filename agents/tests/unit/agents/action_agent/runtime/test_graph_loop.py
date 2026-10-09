from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from pantaray_agents.agents.action_agent.runtime.graph import (
    GraphRecursionError,
    NodeCancelledError,
    build_action_agent_graph,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState

_MARK: contextvars.ContextVar[str] = contextvars.ContextVar("mark", default="caller")


@dataclass
class _FakeRuntime:
    label: str
    state_config: dict[str, int]
    resume_route: str = "init"
    finalize_after_thinks: int = 1
    call_events: list[str] = field(default_factory=list)
    seen: list[ActionAgentState] = field(default_factory=list)
    request: object = field(default_factory=MagicMock)
    emit_action_step: object = field(default_factory=MagicMock)
    emit_error: object = field(default_factory=MagicMock)
    services: object = field(default_factory=MagicMock)

    async def resume_gate(self, state: ActionAgentState) -> ActionAgentState:
        self.seen.append(state)
        return state

    async def init(self, state: ActionAgentState) -> ActionAgentState:
        self.seen.append(state)
        self.call_events.append(f"{self.label}:init")
        state["phase"] = "planning"
        return state

    async def think(self, state: ActionAgentState) -> ActionAgentState:
        self.seen.append(state)
        self.call_events.append(
            f"{self.label}:think={state['phase']}:"
            f"cap={self.state_config['max_parallel_memory_queries']}"
        )
        state["llm_steps_taken"] = state["llm_steps_taken"] + 1
        return state

    async def action(self, state: ActionAgentState) -> ActionAgentState:
        self.seen.append(state)
        self.call_events.append(f"{self.label}:action")
        state["phase"] = "executing"
        return state

    async def finalize(self, state: ActionAgentState) -> ActionAgentState:
        self.seen.append(state)
        self.call_events.append(f"{self.label}:finalize")
        return state

    def route_from_resume(self, _state: ActionAgentState) -> str:
        self.call_events.append(f"{self.label}:route_resume")
        return self.resume_route

    def route_from_think(self, state: ActionAgentState) -> str:
        self.call_events.append(f"{self.label}:route_think={state['phase']}")
        if state["phase"] == "planning":
            return "action"
        if state["llm_steps_taken"] < self.finalize_after_thinks:
            return "think"
        return "finalize"

    def route_after_action(self, _state: ActionAgentState) -> str:
        self.call_events.append(f"{self.label}:route_action")
        return "think"


def _state() -> ActionAgentState:
    return {
        "max_steps": 3,
        "max_tool_steps": 3,
        "step": 0,
        "tool_steps_taken": 0,
        "llm_steps_taken": 0,
    }


@pytest.mark.asyncio
async def test_parallel_runs_keep_their_own_runtime() -> None:
    runtime_a = _FakeRuntime("A", {"max_parallel_memory_queries": 1})
    runtime_b = _FakeRuntime("B", {"max_parallel_memory_queries": 9})
    runner_a = build_action_agent_graph(runtime_a)  # type: ignore[arg-type]
    runner_b = build_action_agent_graph(runtime_b)  # type: ignore[arg-type]

    await asyncio.gather(runner_a(_state()), runner_b(_state()))

    for label, runtime, cap in (("A", runtime_a, 1), ("B", runtime_b, 9)):
        assert runtime.call_events == [
            f"{label}:route_resume",
            f"{label}:init",
            f"{label}:think=planning:cap={cap}",
            f"{label}:route_think=planning",
            f"{label}:action",
            f"{label}:route_action",
            f"{label}:think=executing:cap={cap}",
            f"{label}:route_think=executing",
            f"{label}:finalize",
        ]


@pytest.mark.asyncio
async def test_halt_resume_route_ends_without_running_any_agent_node() -> None:
    runtime = _FakeRuntime("paused", {"max_parallel_memory_queries": 1}, "halt")

    await build_action_agent_graph(runtime)(_state())  # type: ignore[arg-type]

    assert runtime.call_events == ["paused:route_resume"]


@pytest.mark.asyncio
async def test_a_node_return_overwrites_only_the_state_keys_it_carries() -> None:
    """The state merge the Action nodes were written against."""

    runtime = _FakeRuntime("A", {"max_parallel_memory_queries": 1})

    async def init(state: ActionAgentState) -> ActionAgentState:
        state["phase"] = "planning"
        state.pop("final_output")
        state["not_a_state_key"] = 1  # type: ignore[typeddict-unknown-key]
        state["errors"].append("seen by later nodes without a write")  # type: ignore[arg-type]
        return state

    runtime.init = init  # type: ignore[method-assign]
    caller_state = _state() | {"final_output": "kept", "errors": []}

    result = await build_action_agent_graph(runtime)(caller_state)  # type: ignore[arg-type]

    # A popped key keeps its value and a key outside the state is dropped.
    assert result["final_output"] == "kept"
    assert "not_a_state_key" not in result
    assert caller_state["errors"] == ["seen by later nodes without a write"]
    # Every node reads a new dict, in the state's declaration order.
    assert len({id(state) for state in runtime.seen}) == len(runtime.seen)
    assert all(state is not caller_state for state in runtime.seen)
    assert list(result) == [
        key for key in ActionAgentState.__annotations__ if key in result
    ]


@pytest.mark.asyncio
async def test_the_step_that_reaches_the_limit_fails_even_when_it_would_end() -> None:
    # (3 + 3) * 2 + 10 = 22 steps: resume_gate, init, action, finalize and thinks.
    ending_on_last_step = _FakeRuntime("A", {"max_parallel_memory_queries": 1})
    ending_on_last_step.finalize_after_thinks = 18
    ending_before = _FakeRuntime("B", {"max_parallel_memory_queries": 1})
    ending_before.finalize_after_thinks = 17

    with pytest.raises(GraphRecursionError, match="Recursion limit of 22 reached"):
        await build_action_agent_graph(ending_on_last_step)(_state())  # type: ignore[arg-type]
    assert ending_on_last_step.call_events[-1] == "A:finalize"
    result = await build_action_agent_graph(ending_before)(_state())  # type: ignore[arg-type]
    assert result["llm_steps_taken"] == 17


@pytest.mark.asyncio
async def test_a_node_runs_in_its_own_task_and_context() -> None:
    runtime = _FakeRuntime("A", {"max_parallel_memory_queries": 1})
    marks: list[str] = []

    async def init(state: ActionAgentState) -> ActionAgentState:
        _MARK.set("init")
        state["phase"] = "planning"
        return state

    async def finalize(state: ActionAgentState) -> ActionAgentState:
        marks.append(_MARK.get())
        return state

    runtime.init = init  # type: ignore[method-assign]
    runtime.finalize = finalize  # type: ignore[method-assign]

    await build_action_agent_graph(runtime)(_state())  # type: ignore[arg-type]

    assert marks == ["caller"]
    assert _MARK.get() == "caller"


@pytest.mark.asyncio
async def test_a_node_raising_cancelled_error_fails_the_run() -> None:
    runtime = _FakeRuntime("A", {"max_parallel_memory_queries": 1}, "think")

    async def think(_state: ActionAgentState) -> ActionAgentState:
        raise asyncio.CancelledError

    runtime.think = think  # type: ignore[method-assign]

    with pytest.raises(NodeCancelledError, match="Node 'think' raised"):
        await build_action_agent_graph(runtime)(_state())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cancelling_the_run_cancels_the_node() -> None:
    runtime = _FakeRuntime("A", {"max_parallel_memory_queries": 1}, "think")
    started = asyncio.Event()

    async def think(_state: ActionAgentState) -> ActionAgentState:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime.think = think  # type: ignore[method-assign]
    run = asyncio.create_task(build_action_agent_graph(runtime)(_state()))  # type: ignore[arg-type]
    await started.wait()
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run


@pytest.mark.asyncio
async def test_a_route_outside_the_node_s_map_raises_key_error() -> None:
    runtime = _FakeRuntime("A", {"max_parallel_memory_queries": 1}, "think")
    runtime.route_from_think = lambda _state: "halt"  # type: ignore[method-assign]

    with pytest.raises(KeyError, match="halt"):
        await build_action_agent_graph(runtime)(  # type: ignore[arg-type]
            _state() | {"phase": "executing"}
        )

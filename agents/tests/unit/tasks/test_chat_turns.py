"""The chat's turns run one at a time per user, each on a loop of its own."""

from __future__ import annotations

import asyncio
import threading

import pytest

from pantaray_agents.agents.chat_agent.context import ChatWindow
from pantaray_agents.agents.chat_agent.turn import ChatTurnInterrupted, ChatTurnPlan
from pantaray_agents.local_runtime.chat.turn_runs import (
    chat_turn_running,
    stop_chat_turns,
)
from pantaray_agents.local_runtime.runtime.admission import admission_closed
from pantaray_agents.local_runtime.runtime.identity import OwnerMismatchError
from pantaray_agents.tasks import chat_turns
from pantaray_agents.utils.trace_context import get_trace_context


class _Chat:
    """Turns wait while ``waiting`` is positive; each finished run answers one."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.waiting = 0
        self.runs: list[str] = []
        self.loops: set[int] = set()
        self.gate = threading.Event()
        self.started = threading.Event()
        self.interrupt_once = False
        self.hang_once = False
        monkeypatch.setattr(chat_turns.deps, "get_llm_client", object)
        monkeypatch.setattr(chat_turns, "plan_chat_turn", self.plan)
        monkeypatch.setattr(chat_turns, "run_chat_turn", self.run)

    def plan(self, *, user_id: str, retry_of: str | None) -> ChatTurnPlan | None:
        if user_id == "gone":
            raise OwnerMismatchError("owner changed")
        if self.waiting == 0:
            return None
        return ChatTurnPlan(user_id=user_id, key=f"a{len(self.runs)}", cursor=0)

    async def run(
        self, plan: ChatTurnPlan, *, send: object, tools: object, window: ChatWindow
    ) -> ChatWindow:
        # The model client refuses a request that names no user.
        trace = get_trace_context()
        assert trace is not None and trace.user_id == plan.user_id
        self.runs.append(plan.user_id)
        self.loops.add(id(asyncio.get_running_loop()))
        self.started.set()
        if self.hang_once:
            self.hang_once = False
            await asyncio.sleep(60)  # until the barrier stops it
        await asyncio.to_thread(self.gate.wait, 5)
        if self.interrupt_once:
            self.interrupt_once = False
            raise ChatTurnInterrupted("route changed")
        self.waiting -= 1
        return window


async def test_one_turn_runs_at_a_time_off_the_server_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _Chat(monkeypatch)
    chat.waiting = 1
    drain = chat_turns.request_chat_turn("u")
    await asyncio.to_thread(chat.started.wait, 5)
    assert chat_turn_running("u")

    chat.waiting += 1  # the user writes again while the turn runs
    assert chat_turns.request_chat_turn("u") is drain
    chat.gate.set()
    await drain

    assert (chat.runs, chat.waiting) == (["u", "u"], 0)
    assert id(asyncio.get_running_loop()) not in chat.loops
    assert not chat_turn_running("u")


async def test_an_interrupted_turn_runs_again_and_a_departed_owner_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _Chat(monkeypatch)
    chat.waiting = 1
    chat.interrupt_once = True
    chat.gate.set()

    await chat_turns.request_chat_turn("u")
    await chat_turns.request_chat_turn("gone")

    assert (chat.runs, chat.waiting) == (["u", "u"], 0)


async def test_a_stopped_turn_runs_again_only_once_admission_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = _Chat(monkeypatch)
    chat.waiting = 1
    chat.hang_once = True
    chat.gate.set()
    drain = chat_turns.request_chat_turn("u")
    await asyncio.to_thread(chat.started.wait, 5)

    with admission_closed():
        await stop_chat_turns(owner_id="u")
        assert not chat_turn_running("u")
        await asyncio.sleep(0.3)
        assert chat.runs == ["u"]  # held while the identity is swapped
    await drain

    assert (chat.runs, chat.waiting) == (["u", "u"], 0)

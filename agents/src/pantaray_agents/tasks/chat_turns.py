"""Start the chat's turns: one at a time per user, until none waits.

The chat answers in seconds, so its turns do not queue behind the worker's
long jobs. Each user gets one task on the server's loop that plans the turn the
chat waits for and runs it on a thread of its own (``turn_runs``), then plans
again. Asking for a turn while one runs only wakes that task. The chat items
are the whole durable state: a turn the app quit in runs again when a turn is
next asked for, which every new WebSocket session does as it starts.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Final

import pantaray_agents.dependencies as deps
from pantaray_agents.agents.chat_agent.context import ChatWindow
from pantaray_agents.agents.chat_agent.tools import chat_tools
from pantaray_agents.agents.chat_agent.turn import (
    ChatModel,
    ChatTurnInterrupted,
    ChatTurnPlan,
    plan_chat_turn,
    run_chat_turn,
)
from pantaray_agents.local_runtime.chat.turn_runs import run_chat_turn_in_thread
from pantaray_agents.local_runtime.runtime.admission import admission_is_open
from pantaray_agents.local_runtime.runtime.identity import OwnerMismatchError
from pantaray_agents.local_runtime.runtime.job_types import CHAT_TURN_TRACE_TYPE
from pantaray_agents.utils.structured_logging import (
    fingerprint_text,
    log_structured_event,
)
from pantaray_agents.utils.trace_context import TraceContextManager

logger = logging.getLogger(__name__)

# How often a stopped chat looks for the identity change to have landed.
_ADMISSION_POLL_SECONDS: Final[float] = 0.1

_DRAINS: dict[str, asyncio.Task[None]] = {}
_WOKEN: set[str] = set()
_RETRIES: dict[str, str] = {}  # the turn_failure a user asked to run again
_WINDOWS: dict[str, ChatWindow] = {}


def request_chat_turn(
    user_id: str, *, retry_of: str | None = None
) -> asyncio.Task[None]:
    """Make sure the chat's waiting turns run; returns the task running them."""

    if retry_of is not None:
        _RETRIES[user_id] = retry_of
    _WOKEN.add(user_id)
    drain = _DRAINS.get(user_id)
    if drain is None or drain.done():
        drain = asyncio.get_running_loop().create_task(_drain(user_id))
        _DRAINS[user_id] = drain
    return drain


async def _drain(user_id: str) -> None:
    # The set is read only right after an await, so a request made during one
    # is seen, and one made after the last read finds this task done.
    try:
        while user_id in _WOKEN:
            _WOKEN.discard(user_id)
            while planned := await _next_plan(user_id):
                plan, retry_of = planned
                outcome = await asyncio.wrap_future(
                    run_chat_turn_in_thread(user_id, functools.partial(_run, plan))
                )
                if isinstance(outcome, ChatWindow):
                    _WINDOWS[user_id] = outcome
                    # The retry this turn answered is spent; one asked for
                    # meanwhile, or one a stopped turn left, is kept.
                    if _RETRIES.get(user_id) == retry_of:
                        _RETRIES.pop(user_id, None)
    except OwnerMismatchError:
        return
    except Exception as exc:  # noqa: BLE001
        # A turn that could not even record its failure stops here, and a later
        # request for this user starts over from the chat.
        log_structured_event(
            logger,
            level="error",
            evt="CHAT_TURNS_STOPPED",
            component="tasks.chat_turns",
            exception=exc,
            user_id_fp=fingerprint_text(user_id),
        )
        if user_id in _WOKEN:
            # Asked for after this attempt began: that request gets its own.
            _DRAINS[user_id] = asyncio.get_running_loop().create_task(_drain(user_id))


async def _next_plan(user_id: str) -> tuple[ChatTurnPlan, str | None] | None:
    """The turn to run, and the retry it was planned with."""

    # A stop barrier holds admission closed until the new identity is in
    # place; a turn planned before then would start on the old one.
    while not admission_is_open():
        await asyncio.sleep(_ADMISSION_POLL_SECONDS)
    retry_of = _RETRIES.get(user_id)
    plan = await asyncio.to_thread(plan_chat_turn, user_id=user_id, retry_of=retry_of)
    return None if plan is None else (plan, retry_of)


async def _run(plan: ChatTurnPlan) -> ChatWindow | None:
    """One turn on its own loop; None when the route changed under it."""

    try:
        # The model client names every request after the work that sends it.
        with TraceContextManager(
            user_id=plan.user_id,
            local_job_id=f"chat:{plan.item_key}",
            extra={"job_type": CHAT_TURN_TRACE_TYPE},
        ):
            return await run_chat_turn(
                plan,
                send=ChatModel(client=deps.get_llm_client()).send,
                tools=chat_tools(plan),
                window=_WINDOWS.get(plan.user_id) or ChatWindow.fresh(),
            )
    except ChatTurnInterrupted:
        # Planned again: under the new route it runs again, and for an owner
        # who left, the plan refuses.
        return None


__all__ = ["request_chat_turn"]

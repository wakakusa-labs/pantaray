"""Where the chat's turns run, and how a route change stops them.

Each turn gets a daemon thread and an event loop of its own. The shared
conversation loop lays every request out and runs tool bodies on the loop it
is awaited on, so a turn on the server's loop would stall every request and
WebSocket while it works; on its own loop it stalls nothing, and a quitting
app does not wait for it.

The stop barrier closes admission, then calls ``stop_chat_turns`` (registered
with it below) before it swaps the identity, as it cancels the Action runs. A
turn starts only while admission is open, checked under the same lock it is
registered under, so a turn either exists for the barrier to stop or never
starts. A stopped turn is cancelled where it awaits and counts as stopped only
once the work it handed to threads (a database write) has finished too; it
runs again once the new identity is in place.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Final, Literal

from pantaray_agents.local_runtime.runtime.admission import admission_is_open
from pantaray_agents.local_runtime.runtime.identity_stops import (
    stop_on_identity_change,
)

logger = logging.getLogger(__name__)

# How long the barrier waits for a cancelled turn to unwind. A turn stops at its
# next await; a database call it is in finishes first, in milliseconds.
CHAT_TURN_STOP_WAIT_SECONDS: Final[float] = 2.0

type ChatTurnStopped = Literal["stopped"]


@dataclass(frozen=True, slots=True)
class _Run:
    loop: asyncio.AbstractEventLoop
    cancel: Callable[[], object]
    finished: Future[None]


_LOCK = threading.Lock()
_RUNS: dict[str, _Run] = {}


def run_chat_turn_in_thread[T](
    user_id: str, turn: Callable[[], Awaitable[T]]
) -> Future[T | ChatTurnStopped]:
    """Start ``turn`` on a thread of its own; ``"stopped"`` if it was stopped
    or admission was closed. One user runs one turn at a time; the caller
    keeps to that.
    """

    done: Future[T | ChatTurnStopped] = Future()
    finished: Future[None] = Future()

    async def main() -> T:
        return await turn()

    loop = asyncio.new_event_loop()
    with _LOCK:
        if not admission_is_open():
            loop.close()
            done.set_result("stopped")
            return done
        task = loop.create_task(main())
        _RUNS[user_id] = _Run(loop=loop, cancel=task.cancel, finished=finished)

    def run() -> None:
        outcome: T | ChatTurnStopped = "stopped"
        failure: BaseException | None = None
        try:
            outcome = loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except BaseException as exc:  # noqa: BLE001
            # Handed to the waiting caller, which owns the failure.
            failure = exc
        finally:
            # A cancelled await on a thread leaves that thread running: the
            # turn has stopped once those threads have finished too.
            loop.run_until_complete(loop.shutdown_default_executor())
            # Unregistered before the loop closes, under the lock a stop asks
            # under, so a registered loop always takes the stop.
            with _LOCK:
                _RUNS.pop(user_id, None)
            loop.close()
            finished.set_result(None)
        # Last, so the caller's next turn starts only after this one is gone.
        if failure is None:
            done.set_result(outcome)
        else:
            done.set_exception(failure)

    threading.Thread(target=run, name="chat-turn", daemon=True).start()
    return done


def chat_turn_running(user_id: str) -> bool:
    with _LOCK:
        return user_id in _RUNS


async def stop_chat_turns(*, owner_id: str) -> None:
    """Cancel the owner's running turn and wait, briefly, for it to unwind."""

    with _LOCK:
        run = _RUNS.get(owner_id)
        if run is None:
            return
        run.loop.call_soon_threadsafe(run.cancel)
    _, pending = await asyncio.wait(
        {asyncio.wrap_future(run.finished)}, timeout=CHAT_TURN_STOP_WAIT_SECONDS
    )
    if pending:
        # The barrier goes on: what the turn still does is checked against the
        # route and owner it started on.
        logger.warning("A chat turn did not stop within the barrier's wait")


# On import: a chat turn starts only through this module.
stop_on_identity_change(stop_chat_turns)

__all__ = [
    "CHAT_TURN_STOP_WAIT_SECONDS",
    "ChatTurnStopped",
    "chat_turn_running",
    "run_chat_turn_in_thread",
    "stop_chat_turns",
]

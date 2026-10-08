"""Where the chat's turns run, and how a route change stops them.

Each turn gets a daemon thread and an event loop of its own. The shared
conversation loop lays every request out and runs tool bodies on the loop it
is awaited on, so a turn on the server's loop would stall every request and
WebSocket while it works; on its own loop it stalls nothing, and a quitting
app does not wait for it.

The stop barrier calls ``stop_chat_turns`` before it swaps the identity, as it
cancels the Action runs: a running turn is cancelled where it awaits, appends
nothing more, and runs again once the new identity is in place.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Final, Literal

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
    """Start ``turn`` on a thread of its own; ``"stopped"`` if it was stopped.

    One user runs one turn at a time; the caller keeps to that.
    """

    done: Future[T | ChatTurnStopped] = Future()
    finished: Future[None] = Future()

    async def main() -> None:
        task = asyncio.ensure_future(turn())
        with _LOCK:
            _RUNS[user_id] = _Run(
                loop=asyncio.get_running_loop(), cancel=task.cancel, finished=finished
            )
        try:
            done.set_result(await task)
        except asyncio.CancelledError:
            done.set_result("stopped")
        except BaseException as exc:  # noqa: BLE001
            # Handed to the waiting caller, which owns the failure.
            done.set_exception(exc)
        finally:
            with _LOCK:
                _RUNS.pop(user_id, None)
            finished.set_result(None)

    threading.Thread(
        target=asyncio.run, args=(main(),), name="chat-turn", daemon=True
    ).start()
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
    await asyncio.wait(
        {asyncio.wrap_future(run.finished)}, timeout=CHAT_TURN_STOP_WAIT_SECONDS
    )


__all__ = [
    "CHAT_TURN_STOP_WAIT_SECONDS",
    "ChatTurnStopped",
    "chat_turn_running",
    "run_chat_turn_in_thread",
    "stop_chat_turns",
]

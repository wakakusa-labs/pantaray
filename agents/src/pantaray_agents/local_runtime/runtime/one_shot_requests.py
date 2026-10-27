"""One-shot rendezvous between an Action tool and Electron main.

The Action runtime, the local backend HTTP app and the orchestration WebSocket
share one process, so a request the desktop app has to fulfil is held in memory:
the tool opens a request, hands its id to the owner's live WebSocket session
(``deliver_desktop_request``), and Electron answers it with a
``POST /local/...`` endpoint.

A request id is consumed exactly once. A second answer is rejected rather than
overwriting the first, so a replayed POST cannot swap a result a tool already
verified, and an answer for another user is refused before it is read.

Design limit: a pending request does not survive a local backend restart. The
waiting tool then times out, which is the same fail-closed result as an Electron
that never answers.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Literal

type OneShotAnswerStatus = Literal[
    "accepted",
    "unknown_request",
    "user_mismatch",
    "already_answered",
]


@dataclass(slots=True)
class _Pending[T]:
    user_id: str
    outcome: asyncio.Future[T] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class OneShotRequests[T]:
    """One-shot requests keyed by a server-issued id."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending[T]] = {}

    def open(self, *, user_id: str) -> str:
        request_id = str(uuid.uuid4())
        self._pending[request_id] = _Pending(user_id=user_id)
        return request_id

    async def wait(self, *, request_id: str, timeout_seconds: float) -> T | None:
        """Await the answer, or return None once the deadline passes.

        The request is closed either way: a late answer finds nothing to resolve
        instead of resolving a request nobody is waiting on any more.
        """

        pending = self._pending.get(request_id)
        if pending is None:
            raise KeyError(request_id)
        try:
            return await asyncio.wait_for(pending.outcome, timeout=timeout_seconds)
        except TimeoutError:
            return None
        finally:
            self.close(request_id=request_id)

    def answer(
        self,
        *,
        request_id: str,
        user_id: str,
        outcome: T,
    ) -> OneShotAnswerStatus:
        pending = self._pending.get(request_id)
        if pending is None:
            return "unknown_request"
        if pending.user_id != user_id:
            return "user_mismatch"
        if pending.outcome.done():
            return "already_answered"
        pending.outcome.set_result(outcome)
        return "accepted"

    def close(self, *, request_id: str) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is not None and not pending.outcome.done():
            pending.outcome.cancel()


__all__ = [
    "OneShotAnswerStatus",
    "OneShotRequests",
]

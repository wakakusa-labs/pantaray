"""Tell the owner's live WS session that their TODOs changed.

`todo_revisions` counts every change to `todo_items`, whoever made it, so the
session follows that counter from the value it read when it started and sends
`todo_changed { revision }` when it moves. The event carries no TODO content: the
screen reads the list again over HTTP, as it does once `session_started` arrives.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Final

import pantaray_agents.dependencies as deps
from pantaray_agents.local_runtime.todos.store import read_todo_revision
from pantaray_agents.orchestration.ws.background_task import spawn_ws_background_task
from pantaray_agents.orchestration.ws.base import BaseWSHandler
from pantaray_agents.orchestration.ws.task_supervisor import WsTaskSupervisor
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.todos import TodoChangedMessage
from pantaray_agents.utils.ws_observability import RateLimiter

logger = logging.getLogger(__name__)

TODO_RELAY_TASK_KEY: Final[str] = "todo_relay_tick"
# An AI's check or report should show within a second; the read is one row by
# primary key per live session.
TODO_RELAY_TICK_SECONDS: Final[float] = 0.5
_RELAY_FAILURE_LOG_INTERVAL_SECONDS: Final[float] = 60.0


class TodoRelayMixin(BaseWSHandler):
    _rate_limiter: RateLimiter
    _task_supervisor: WsTaskSupervisor

    def start_todo_relay(self) -> None:
        """Read where the relay starts right after `session_started` is sent.

        A change after this read is sent; one before it is in what the client
        reads after `session_started`. A failed read ends the session, as the
        chat relay's does, so the client reconnects and reads again.
        """

        if deps.is_mock_mode():
            # Mock mode runs without the runtime database.
            return
        seen = read_todo_revision(user_id=str(self.user_id))
        spawn_ws_background_task(
            owner=self,
            task_key=TODO_RELAY_TASK_KEY,
            coro=self._todo_relay_loop(seen),
        )

    async def _todo_relay_loop(self, seen: int) -> None:
        user_id = str(self.user_id)
        try:
            while not self._is_closed:
                try:
                    revision = await asyncio.to_thread(
                        read_todo_revision, user_id=user_id
                    )
                except sqlite3.Error as exc:
                    # A failed read must not end the session; the next tick
                    # compares with the last revision sent.
                    if self._rate_limiter.should_log(
                        "todo_relay:tick",
                        interval_seconds=_RELAY_FAILURE_LOG_INTERVAL_SECONDS,
                    ):
                        logger.warning(
                            "TODO relay tick failed: session_id=%s error=%s",
                            self.session_id,
                            type(exc).__name__,
                        )
                else:
                    if revision != seen:
                        if not await self._send(
                            OutboundEvent.TODO_CHANGED.value,
                            TodoChangedMessage(revision=revision),
                            store_in_session_store=False,
                            persist_public_event=False,
                        ):
                            return
                        seen = revision
                await asyncio.sleep(TODO_RELAY_TICK_SECONDS)
        except asyncio.CancelledError:
            return

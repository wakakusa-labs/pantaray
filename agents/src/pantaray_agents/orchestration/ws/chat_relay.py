"""Relay items appended to the owner's chat over the live WS session.

`chat_items` is the source of truth and any thread may append to it, so the
session follows the table from the newest item it saw when it started. A client
reads the chat over HTTP once `session_started` arrives; an item it also gets
from the relay is the same item, by `item_id`.

Each tick also sends `chat_turn_state` when a turn starts or ends, and once at
the start. It is sampled before the items are read and sent after them, so a
turn's end follows its reply; a failed read holds both back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

import pantaray_agents.dependencies as deps
from pantaray_agents.local_runtime.chat.store import (
    read_chat_items_after,
    read_latest_chat_sequence,
)
from pantaray_agents.local_runtime.chat.turn_runs import chat_turn_running
from pantaray_agents.orchestration.ws.background_task import spawn_ws_background_task
from pantaray_agents.orchestration.ws.base import BaseWSHandler
from pantaray_agents.orchestration.ws.task_supervisor import WsTaskSupervisor
from pantaray_agents.schema.chat import ChatItemAppendedMessage, ChatTurnStateMessage
from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.utils.ws_observability import RateLimiter

logger = logging.getLogger(__name__)

CHAT_RELAY_TASK_KEY: Final[str] = "chat_relay_tick"
# A chat reply should appear as soon as it is written; one indexed read per
# tick per live session is cheap next to that.
CHAT_RELAY_TICK_SECONDS: Final[float] = 0.25
_RELAY_FAILURE_LOG_INTERVAL_SECONDS: Final[float] = 60.0


class ChatRelayMixin(BaseWSHandler):
    _rate_limiter: RateLimiter
    _task_supervisor: WsTaskSupervisor

    def start_chat_relay(self) -> None:
        """Fix where the relay starts, then follow the chat from there.

        Called synchronously right after `session_started` is sent: no request
        is served before this read, so a client that reads the chat after
        `session_started` has every item the relay will not send. A failed
        read ends the session instead: the client reconnects and reads again,
        where starting later would skip the items appended in between.
        """

        if deps.is_mock_mode():
            # Mock mode runs without the runtime database.
            return
        after = read_latest_chat_sequence(user_id=str(self.user_id))
        spawn_ws_background_task(
            owner=self,
            task_key=CHAT_RELAY_TASK_KEY,
            coro=self._chat_relay_loop(after),
        )

    def stop_chat_relay(self) -> None:
        """Cancel without waiting, as the close path stops the Suggestion relay."""

        self._task_supervisor.cancel(CHAT_RELAY_TASK_KEY)

    async def _chat_relay_loop(self, after: int) -> None:
        user_id = str(self.user_id)
        sent_running: bool | None = None
        try:
            while not self._is_closed:
                # Sampled before the read and sent after it: a turn appends its
                # end before it stops running, so its reply goes out first.
                running = chat_turn_running(user_id)
                try:
                    for item in await asyncio.to_thread(
                        read_chat_items_after, user_id=user_id, after=after
                    ):
                        if not await self._send(
                            OutboundEvent.CHAT_ITEM_APPENDED.value,
                            ChatItemAppendedMessage(item=item),
                            store_in_session_store=False,
                            persist_public_event=False,
                        ):
                            return
                        after = item.sequence
                    if running != sent_running:
                        if not await self._send(
                            OutboundEvent.CHAT_TURN_STATE.value,
                            ChatTurnStateMessage(running=running),
                            store_in_session_store=False,
                            persist_public_event=False,
                        ):
                            return
                        sent_running = running
                except Exception as exc:  # noqa: BLE001
                    # A failed read must not end the session; the next tick
                    # resumes from the last item delivered.
                    if self._rate_limiter.should_log(
                        "chat_relay:tick",
                        interval_seconds=_RELAY_FAILURE_LOG_INTERVAL_SECONDS,
                    ):
                        logger.warning(
                            "Chat relay tick failed: session_id=%s error=%s",
                            self.session_id,
                            exc,
                            exc_info=True,
                        )
                await asyncio.sleep(CHAT_RELAY_TICK_SECONDS)
        except asyncio.CancelledError:
            return

"""The WebSocket sessions that can deliver to their owner right now.

A Suggestion that no session can deliver is discarded instead of stored, so
this registry is the save condition for the Suggestion job and the welcome.
The socket ledger is not: a socket is bound at handshake, before its session's
relay has fixed the time from which it delivers finished Suggestions, and a
Suggestion saved in that gap would never be relayed. A session joins once its
relay runs, leaves when it closes, and counts only while it can still send.
The runtime consults this registry and the WebSocket layer fills it, so it sits
with the runtime.

A desktop request (a screen capture, an HTML render) is handed to the newest of
these sessions, whichever Action asked and whether or not a session follows that
Action. The asking tool runs on a worker thread with its own event loop, so each
session is recorded with the loop that serves it and the send runs there.
"""

from __future__ import annotations

import asyncio
import logging
from threading import Lock
from typing import Protocol

from pantaray_agents.schema.events import OutboundEvent
from pantaray_agents.schema.websocket.server_messages import (
    HtmlRenderRequestedMessage,
    ScreenCaptureRequestedMessage,
)

logger = logging.getLogger(__name__)

type DesktopRequestMessage = ScreenCaptureRequestedMessage | HtmlRenderRequestedMessage

_DESKTOP_REQUEST_EVENTS: dict[type[DesktopRequestMessage], OutboundEvent] = {
    ScreenCaptureRequestedMessage: OutboundEvent.SCREEN_CAPTURE_REQUESTED,
    HtmlRenderRequestedMessage: OutboundEvent.HTML_RENDER_REQUESTED,
}


class DeliverableSession(Protocol):
    def can_deliver(self) -> bool: ...

    async def send_desktop_request(
        self, event: OutboundEvent, message: DesktopRequestMessage
    ) -> bool: ...


_LOCK = Lock()
# Insertion order is connection order, so an owner's newest session is last.
_SESSIONS_BY_OWNER: dict[str, dict[DeliverableSession, asyncio.AbstractEventLoop]] = {}


def register_deliverable_session(owner_id: str, session: DeliverableSession) -> None:
    """Called on the loop that serves the session."""
    loop = asyncio.get_running_loop()
    with _LOCK:
        _SESSIONS_BY_OWNER.setdefault(owner_id, {})[session] = loop


def unregister_deliverable_session(owner_id: str, session: DeliverableSession) -> None:
    """Idempotent, so every close path can call it."""
    with _LOCK:
        sessions = _SESSIONS_BY_OWNER.get(owner_id)
        if sessions is None:
            return
        sessions.pop(session, None)
        if not sessions:
            del _SESSIONS_BY_OWNER[owner_id]


def owner_has_deliverable_session(owner_id: str) -> bool:
    """Whether at least one of the owner's sessions can deliver right now.

    Called from worker threads as well as the serving loop; a session's state
    only ever changes from able to send to closed.
    """
    with _LOCK:
        sessions = tuple(_SESSIONS_BY_OWNER.get(owner_id, ()))
    return any(session.can_deliver() for session in sessions)


async def deliver_desktop_request(
    owner_id: str, message: DesktopRequestMessage
) -> None:
    """Send a desktop request through the owner's newest session that can send.

    One session only: each delivery makes the desktop capture or render once,
    and only the first answer is accepted. The newest is the one the desktop
    reads, because Electron main keeps a single socket and ignores one it has
    replaced; an older session still listed is a socket whose close the server
    has not seen yet. Nothing is queued, so a request no session receives fails
    the way an unanswered one does, when the tool's wait times out.

    Awaitable from any thread and any event loop.
    """
    with _LOCK:
        sessions = list(_SESSIONS_BY_OWNER.get(owner_id, {}).items())
    event = _DESKTOP_REQUEST_EVENTS[type(message)]
    for session, loop in reversed(sessions):
        if session.can_deliver():
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(
                    session.send_desktop_request(event, message), loop
                )
            )
            return
    logger.warning("No desktop session to deliver a request to: event=%s", event)

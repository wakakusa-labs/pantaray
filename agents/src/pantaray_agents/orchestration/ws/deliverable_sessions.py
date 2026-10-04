"""The WebSocket sessions that can deliver a Suggestion to their owner right now.

A Suggestion that no session can deliver is discarded instead of stored, so
this registry is the save condition for the Suggestion job and the welcome.
The socket ledger is not: a socket is bound at handshake, before its session's
relay has fixed the time from which it delivers finished Suggestions, and a
Suggestion saved in that gap would never be relayed. A session joins once its
relay runs, leaves when it closes, and counts only while it can still send.
"""

from __future__ import annotations

from threading import Lock
from typing import Protocol


class DeliverableSession(Protocol):
    def can_deliver(self) -> bool: ...


_LOCK = Lock()
_SESSIONS_BY_OWNER: dict[str, set[DeliverableSession]] = {}


def register_deliverable_session(owner_id: str, session: DeliverableSession) -> None:
    with _LOCK:
        _SESSIONS_BY_OWNER.setdefault(owner_id, set()).add(session)


def unregister_deliverable_session(owner_id: str, session: DeliverableSession) -> None:
    """Idempotent, so every close path can call it."""
    with _LOCK:
        sessions = _SESSIONS_BY_OWNER.get(owner_id)
        if sessions is None:
            return
        sessions.discard(session)
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

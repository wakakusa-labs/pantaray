"""Which chat messages the next unified Memory run is handed.

The chat is a source of its own: a run is handed the messages after the user's
chat cursor, pinned in its job payload as a sequence range, and the cursor moves
to the range's end when that job is enqueued, as a trigger is marked dispatched
then. A failed run therefore loses its range as it loses its triggers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from pantaray_agents.local_runtime.storage.chat_messages import read_chat_messages
from pantaray_agents.tasks.types import MemoryUpdateChatRange

from .job_payload_models import MEMORY_UPDATE_SOURCE_MAX_ITEMS

# Design limit: one run reads at most this many characters of chat (about
# 20-30k tokens) beside its other evidence, and a longer backlog waits for the
# next runs. Raise it if chat backlogs are seen to lag behind by several runs.
MEMORY_CHAT_MAX_CHARS: Final[int] = 60_000


@dataclass(frozen=True, slots=True)
class PendingChat:
    range: MemoryUpdateChatRange
    # When the oldest message of the range was said.
    oldest_created_at: str


def pending_chat(connection: sqlite3.Connection, *, user_id: str) -> PendingChat | None:
    """The messages after the cursor that fit one run, or None when none wait.

    A message longer than the character bound still goes alone, so the
    cursor always moves.
    """

    after = _cursor(connection, user_id=user_id)
    messages = read_chat_messages(
        connection,
        user_id=user_id,
        after=after,
        limit=MEMORY_UPDATE_SOURCE_MAX_ITEMS,
    )
    if not messages:
        return None
    through = messages[0].sequence
    total = 0
    for message in messages:
        total += len(message.content.text)
        if total > MEMORY_CHAT_MAX_CHARS and message is not messages[0]:
            break
        through = message.sequence
    return PendingChat(
        range={"after_sequence": after, "through_sequence": through},
        oldest_created_at=messages[0].created_at,
    )


def advance_chat_cursor(
    connection: sqlite3.Connection, *, user_id: str, through: int, at: str
) -> None:
    connection.execute(
        """
        INSERT INTO memory_chat_cursors(user_id, through_sequence, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            through_sequence = excluded.through_sequence,
            updated_at = excluded.updated_at
        """,
        (user_id, through, at),
    )


def _cursor(connection: sqlite3.Connection, *, user_id: str) -> int:
    row = connection.execute(
        "SELECT through_sequence FROM memory_chat_cursors WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return 0 if row is None else int(row[0])


__all__ = [
    "MEMORY_CHAT_MAX_CHARS",
    "PendingChat",
    "advance_chat_cursor",
    "pending_chat",
]

"""Which chat messages the next unified Memory run is handed.

The chat is a source of its own: each message is an event in the Memory
update hook's inbox, and a run is handed the messages from the oldest pending
one, pinned in its job payload as a sequence range. The events of the range
are consumed when that job is enqueued, so a failed run loses its range as it
loses its other sources.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from pantaray_agents.local_runtime.storage.chat_messages import read_chat_messages
from pantaray_agents.tasks.types import MemoryUpdateChatRange

from .job_payload_models import MEMORY_UPDATE_SOURCE_MAX_ITEMS

# Design limit: one run reads at most this many characters of chat (about
# 20-30k tokens) beside its other evidence, and a longer backlog waits for the
# next runs. Raise it if chat backlogs are seen to lag behind by several runs.
MEMORY_CHAT_MAX_CHARS: Final[int] = 60_000


def chat_range(
    connection: sqlite3.Connection, *, user_id: str, after: int
) -> MemoryUpdateChatRange:
    """The messages after ``after`` that fit one run.

    A message longer than the character bound still goes alone, so the
    backlog always moves. Chat messages are never deleted, so a pending
    message event always finds its message.
    """

    messages = read_chat_messages(
        connection,
        user_id=user_id,
        after=after,
        limit=MEMORY_UPDATE_SOURCE_MAX_ITEMS,
    )
    through = messages[0].sequence
    total = 0
    for message in messages:
        total += len(message.content.text)
        if total > MEMORY_CHAT_MAX_CHARS and message is not messages[0]:
            break
        through = message.sequence
    return {"after_sequence": after, "through_sequence": through}


__all__ = ["MEMORY_CHAT_MAX_CHARS", "chat_range"]

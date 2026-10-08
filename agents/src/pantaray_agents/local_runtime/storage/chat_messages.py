"""The user's and Pantaray's messages in the single chat, for readers below it.

The chat (`local_runtime/chat`) owns appending items. Memory runs read what was
said there through this module, which sits below both, so the runtime that
dispatches them never loads the chat. Events, cards' own items and turn
failures are not messages and are not read here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pantaray_agents.schema.chat import (
    AssistantMessageContent,
    ChatItem,
    UserMessageContent,
)

CHAT_MESSAGE_KINDS = ("user_message", "assistant_message")
_KINDS_SQL = ", ".join(f"'{kind}'" for kind in CHAT_MESSAGE_KINDS)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    sequence: int
    item_id: str
    created_at: str
    content: UserMessageContent | AssistantMessageContent


def read_chat_messages(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    after: int,
    through: int | None = None,
    limit: int | None = None,
) -> tuple[ChatMessage, ...]:
    """Messages with a sequence in (`after`, `through`], oldest first."""

    rows = connection.execute(
        "SELECT sequence, item_id, created_at, payload FROM chat_items "
        f"WHERE user_id = ? AND kind IN ({_KINDS_SQL}) AND sequence > ? "
        "AND (? IS NULL OR sequence <= ?) ORDER BY sequence LIMIT ?",
        (user_id, after, through, through, -1 if limit is None else limit),
    ).fetchall()
    return tuple(_message(row) for row in rows)


def _message(row: sqlite3.Row | tuple[object, ...]) -> ChatMessage:
    sequence, item_id, created_at, payload = row[0], row[1], row[2], row[3]
    item = ChatItem.model_validate_json(
        json.dumps(
            {
                "sequence": sequence,
                "item_id": item_id,
                "created_at": created_at,
                "content": json.loads(str(payload)),
            }
        )
    )
    # The query selects message kinds only.
    assert isinstance(item.content, UserMessageContent | AssistantMessageContent)
    return ChatMessage(
        sequence=item.sequence,
        item_id=item.item_id,
        created_at=item.created_at,
        content=item.content,
    )


__all__ = ["CHAT_MESSAGE_KINDS", "ChatMessage", "read_chat_messages"]

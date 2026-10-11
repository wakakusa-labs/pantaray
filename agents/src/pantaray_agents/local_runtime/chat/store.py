"""Append-only storage of the user's chat items.

Every item is appended under an idempotency key (`message_id`): appending the
same key again returns the item already stored, and reusing it for different
content is refused. Nothing here updates or deletes an item.

A chat turn keys what it appends under `chat-turn/{turn_key}/...`, and the item
that ends a turn -- its reply, or its failure -- names in its key the newest
item the turn read (`.../reply/{read_through}`, `.../failure/{read_through}`).
That is the only record of what the turns have answered: a trigger item past
the last end's `read_through` still waits for a turn.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Literal

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_regular_file_descriptor,
)
from pantaray_agents.local_runtime.runtime.action_file_attachments import (
    STAGED_ATTACHMENT_DIRECTORY,
)
from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row
from pantaray_agents.schema.chat import (
    AssistantMessageContent,
    ChatActionCard,
    ChatItem,
    ChatItemContent,
    ChatItemPage,
    ChatMessageRejectedField,
    UserMessageContent,
)
from pantaray_agents.security.storage_paths import validate_image_storage_path

_ITEM_COLUMNS = "sequence, item_id, created_at, payload"
_TURN_MESSAGE_PREFIX = "chat-turn/"
# The kinds a turn answers; the others are what a turn itself appends.
CHAT_TRIGGER_KINDS = ("user_message", "action_event")
_TRIGGER_KINDS_SQL = ", ".join(f"'{kind}'" for kind in CHAT_TRIGGER_KINDS)
# A suggestion's arrival, no longer written: rows a store still holds are left
# out of every read.
_SHOWN_SQL = "kind != 'suggestion_event'"
# A turn's ends, as their keys spell them. Only a turn appends these kinds, so
# a user message whose client chose a key of the same shape is never one.
_REPLY_END_SQL = (
    "(kind = 'assistant_message' "
    f"AND message_id GLOB '{_TURN_MESSAGE_PREFIX}*/reply/*')"
)
_END_SQL = f"(kind = 'turn_failure' OR {_REPLY_END_SQL})"

type ChatTurnEnd = Literal["reply", "failure"]


class ChatItemReferenceError(ValueError):
    """The item names a quote, an image or a file the user does not have."""

    def __init__(self, field: ChatMessageRejectedField) -> None:
        super().__init__(f"chat item references an unavailable {field}")
        self.field: ChatMessageRejectedField = field


class ChatMessageIdentityConflictError(ValueError):
    """The idempotency key was already used for different content."""


def append_chat_item(
    *, user_id: str, message_id: str, content: ChatItemContent
) -> ChatItem:
    """Append one item, or return the one already appended under `message_id`.

    Raises:
        ChatItemReferenceError: a quote, image or file cannot be used.
        ChatMessageIdentityConflictError: `message_id` holds other content.
    """

    verify_current_owner(user_id)
    with _connection() as connection, immediate_transaction(connection):
        existing = connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM chat_items "
            "WHERE user_id = ? AND message_id = ?",
            (user_id, message_id),
        ).fetchone()
        if existing is not None:
            item = _item(existing)
            if item.content != content:
                raise ChatMessageIdentityConflictError(
                    "message_id was already used for a different item"
                )
            return item
        # Checked only for a first append: a retry after a lost response must
        # not depend on staged files a later Action may already have taken.
        quote_item_id = _require_references(
            connection=connection, user_id=user_id, content=content
        )
        created_at = now_utc_iso()
        ensure_user_row(connection, user_id=user_id, timestamp=created_at)
        row = connection.execute(
            "INSERT INTO chat_items("
            "item_id, user_id, message_id, kind, quote_item_id, payload, created_at"
            f") VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING {_ITEM_COLUMNS}",
            (
                str(uuid.uuid4()),
                user_id,
                message_id,
                content.kind,
                quote_item_id,
                content.model_dump_json(),
                created_at,
            ),
        ).fetchone()
        return _item(row)


def read_chat_page(*, user_id: str, before: int | None, limit: int) -> ChatItemPage:
    """Read up to `limit` items older than `before` (the newest when omitted)."""

    with _connection() as connection:
        rows = connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM chat_items "
            f"WHERE user_id = ? AND (? IS NULL OR sequence < ?) AND {_SHOWN_SQL} "
            "ORDER BY sequence DESC LIMIT ?",
            (user_id, before, before, limit + 1),
        ).fetchall()
    items = tuple(_item(row) for row in rows[:limit])
    return ChatItemPage(
        items=items,
        next_cursor=items[-1].sequence if len(rows) > limit else None,
    )


def read_chat_items_after(*, user_id: str, after: int) -> tuple[ChatItem, ...]:
    """Read every item appended after `after`, oldest first."""

    with _connection() as connection:
        rows = connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM chat_items "
            f"WHERE user_id = ? AND sequence > ? AND {_SHOWN_SQL} ORDER BY sequence",
            (user_id, after),
        ).fetchall()
    return tuple(_item(row) for row in rows)


@dataclass(frozen=True, slots=True)
class TurnChatItem:
    """An item as a turn reads it: ``is_reply`` marks an end a turn replied with."""

    item: ChatItem
    is_reply: bool


@dataclass(frozen=True, slots=True)
class ChatTurnMarks:
    """What the turns have answered, read back from their last ends.

    ``answered_through`` is the ``read_through`` of the last end, a reply or a
    failure, and ``replied_through`` that of the last reply. ``last_failure``
    is the last end when it is a failure with input a retry can answer again, and
    ``failed_turn_key`` the key of the turn that failed; ``waiting`` says a
    trigger item past ``answered_through`` waits for a turn.
    """

    answered_through: int
    replied_through: int
    last_failure: ChatItem | None
    failed_turn_key: str | None
    waiting: bool


def chat_turn_message_id(turn_key: str, part: str) -> str:
    return f"{_TURN_MESSAGE_PREFIX}{turn_key}/{part}"


def chat_turn_end_message_id(turn_key: str, end: ChatTurnEnd, read_through: int) -> str:
    return chat_turn_message_id(turn_key, f"{end}/{read_through}")


def read_chat_items_for_turn(*, user_id: str, after: int) -> tuple[TurnChatItem, ...]:
    """Read every item appended after `after`, oldest first, as a turn reads it."""

    with _connection() as connection:
        rows = connection.execute(
            f"SELECT {_ITEM_COLUMNS}, {_REPLY_END_SQL} AS is_reply "
            "FROM chat_items WHERE user_id = ? AND sequence > ? "
            f"AND {_SHOWN_SQL} ORDER BY sequence",
            (user_id, after),
        ).fetchall()
    return tuple(
        TurnChatItem(item=_item(row), is_reply=bool(row["is_reply"])) for row in rows
    )


def read_chat_turn_marks(*, user_id: str) -> ChatTurnMarks:
    """Read where the turns stand; an empty chat has answered through 0."""

    with _connection() as connection:
        last_end = _read_last(connection, user_id=user_id, end_sql=_END_SQL)
        failed = last_end is not None and last_end["kind"] == "turn_failure"
        last_reply = (
            _read_last(connection, user_id=user_id, end_sql=_REPLY_END_SQL)
            if failed
            else last_end
        )
        answered_through = _read_through(last_end)
        replied_through = _read_through(last_reply)
        waiting = _has_trigger_after(
            connection, user_id=user_id, after=answered_through
        )
        # A failure whose only input was a suggestion's arrival, no longer
        # read, has nothing a retry could answer: it is not offered again.
        retryable = failed and _has_trigger_after(
            connection, user_id=user_id, after=replied_through
        )
    return ChatTurnMarks(
        answered_through=answered_through,
        replied_through=replied_through,
        last_failure=_item(last_end) if retryable and last_end is not None else None,
        failed_turn_key=_turn_key(last_end)
        if retryable and last_end is not None
        else None,
        waiting=waiting,
    )


def _has_trigger_after(
    connection: sqlite3.Connection, *, user_id: str, after: int
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM chat_items WHERE user_id = ? AND sequence > ? "
        f"AND kind IN ({_TRIGGER_KINDS_SQL}) LIMIT 1",
        (user_id, after),
    ).fetchone()
    return row is not None


def read_unavailable_reference(
    *, user_id: str, content: AssistantMessageContent
) -> Literal["quote_item_id", "cards"] | None:
    """Name what a reply refers to that the user does not have, if anything.

    Cards are checked here rather than on append: a turn checks its reply
    before it ends on it, and nothing else appends a card.
    """

    with _connection() as connection:
        try:
            _require_references(connection=connection, user_id=user_id, content=content)
        except ChatItemReferenceError:
            return "quote_item_id"
        if not _card_targets_exist(
            connection=connection, user_id=user_id, content=content
        ):
            return "cards"
    return None


def read_user_message(*, user_id: str, item_id: str) -> UserMessageContent | None:
    """The user's message ``item_id``, or None when it is not one of theirs."""

    with _connection() as connection:
        row = connection.execute(
            f"SELECT {_ITEM_COLUMNS} FROM chat_items "
            "WHERE user_id = ? AND item_id = ? AND kind = 'user_message'",
            (user_id, item_id),
        ).fetchone()
    if row is None:
        return None
    content = _item(row).content
    assert isinstance(content, UserMessageContent)
    return content


def read_latest_chat_sequence(*, user_id: str) -> int:
    """The newest item's sequence, or 0 for an empty chat."""

    with _connection() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM chat_items WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row[0])


def _read_last(
    connection: sqlite3.Connection, *, user_id: str, end_sql: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        f"SELECT {_ITEM_COLUMNS}, kind, message_id FROM chat_items "
        f"WHERE user_id = ? AND {end_sql} ORDER BY sequence DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row


def _turn_key(end: sqlite3.Row) -> str:
    # `chat-turn/{key}[.{attempt}]/{end}/{read_through}`
    return str(end["message_id"]).split("/")[1].split(".")[0]


def _read_through(end: sqlite3.Row | None) -> int:
    return 0 if end is None else int(str(end["message_id"]).rsplit("/", 1)[1])


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        configure_connection(connection, busy_timeout_ms)
        yield connection


def _item(row: sqlite3.Row) -> ChatItem:
    return ChatItem.model_validate_json(
        json.dumps(
            {
                "sequence": row["sequence"],
                "item_id": row["item_id"],
                "created_at": row["created_at"],
                "content": json.loads(row["payload"]),
            }
        )
    )


def _require_references(
    *, connection: sqlite3.Connection, user_id: str, content: ChatItemContent
) -> str | None:
    """Return the quoted item id once every reference the item makes is usable."""

    if not isinstance(content, UserMessageContent | AssistantMessageContent):
        return None
    quote_item_id = content.quote_item_id
    if quote_item_id is not None and (
        connection.execute(
            # Only a message is drawn, so only a message can be quoted.
            "SELECT 1 FROM chat_items WHERE user_id = ? AND item_id = ? "
            "AND kind IN ('user_message', 'assistant_message')",
            (user_id, quote_item_id),
        ).fetchone()
        is None
    ):
        raise ChatItemReferenceError("quote_item_id")
    if isinstance(content, UserMessageContent):
        _require_attachments(user_id=user_id, content=content)
    return quote_item_id


def _card_targets_exist(
    *, connection: sqlite3.Connection, user_id: str, content: AssistantMessageContent
) -> bool:
    for card in content.cards:
        table, column, target = (
            ("agent_actions", "action_id", card.action_id)
            if isinstance(card, ChatActionCard)
            else ("agent_suggestions", "suggestion_id", card.suggestion_id)
        )
        if (
            connection.execute(
                f"SELECT 1 FROM {table} WHERE user_id = ? AND {column} = ?",
                (user_id, target),
            ).fetchone()
            is None
        ):
            return False
    return True


def _require_attachments(*, user_id: str, content: UserMessageContent) -> None:
    """Hold attachments to the checks an Action submission applies to them.

    The staged files stay where Electron main wrote them: handing one to an
    Action is what moves it into that Action's workspace.
    """

    for image in content.images:
        try:
            validate_image_storage_path(
                user_id=user_id, storage_path=image.storage_path
            )
        except ValueError as exc:
            raise ChatItemReferenceError("images") from exc
    if not content.files:
        return
    artifact_root = read_local_runtime_artifact_root()
    for file in content.files:
        try:
            descriptor = open_regular_file_descriptor(
                root_path=artifact_root,
                relative_path=(
                    f"{STAGED_ATTACHMENT_DIRECTORY}/{user_id}/"
                    f"{file.attachment_id}{file.extension}"
                ),
            )
        except DescriptorPathError as exc:
            raise ChatItemReferenceError("files") from exc
        try:
            size = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
        if size != file.byte_size:
            raise ChatItemReferenceError("files")


__all__ = [
    "CHAT_TRIGGER_KINDS",
    "ChatItemReferenceError",
    "ChatMessageIdentityConflictError",
    "ChatTurnEnd",
    "ChatTurnMarks",
    "TurnChatItem",
    "append_chat_item",
    "chat_turn_end_message_id",
    "chat_turn_message_id",
    "read_chat_items_after",
    "read_chat_items_for_turn",
    "read_chat_page",
    "read_chat_turn_marks",
    "read_latest_chat_sequence",
    "read_unavailable_reference",
    "read_user_message",
]

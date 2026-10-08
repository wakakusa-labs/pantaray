"""Append-only storage of the user's chat items.

Every item is appended under an idempotency key (`message_id`): appending the
same key again returns the item already stored, and reusing it for different
content is refused. Nothing here updates or deletes an item.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager

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
    ChatItem,
    ChatItemContent,
    ChatItemPage,
    ChatMessageRejectedField,
    UserMessageContent,
)
from pantaray_agents.security.storage_paths import validate_image_storage_path

_ITEM_COLUMNS = "sequence, item_id, created_at, payload"


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
            "WHERE user_id = ? AND (? IS NULL OR sequence < ?) "
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
            "WHERE user_id = ? AND sequence > ? ORDER BY sequence",
            (user_id, after),
        ).fetchall()
    return tuple(_item(row) for row in rows)


def read_latest_chat_sequence(*, user_id: str) -> int:
    """The newest item's sequence, or 0 for an empty chat."""

    with _connection() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM chat_items WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row[0])


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
            "SELECT 1 FROM chat_items WHERE user_id = ? AND item_id = ?",
            (user_id, quote_item_id),
        ).fetchone()
        is None
    ):
        raise ChatItemReferenceError("quote_item_id")
    if isinstance(content, UserMessageContent):
        _require_attachments(user_id=user_id, content=content)
    return quote_item_id


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
    "ChatItemReferenceError",
    "ChatMessageIdentityConflictError",
    "append_chat_item",
    "read_chat_items_after",
    "read_chat_page",
    "read_latest_chat_sequence",
]

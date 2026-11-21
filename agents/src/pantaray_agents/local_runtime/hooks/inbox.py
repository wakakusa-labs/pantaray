"""The hooks' inboxes in the local runtime database: written, read, consumed."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from contextlib import closing
from functools import cache
from pathlib import Path

from pantaray_agents.hooks.definitions import EventValue, inbox_events
from pantaray_agents.hooks.engine import InboxEvent
from pantaray_agents.local_runtime.activity_summary_schedule import parse_iso_z
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from .builtin import builtin_hooks
from .events import HookEvent


@cache
def _listeners() -> Mapping[str, tuple[str, ...]]:
    listeners: dict[str, list[str]] = {}
    for hook in builtin_hooks():
        for name in inbox_events(hook):
            listeners.setdefault(name, []).append(hook.id)
    return {name: tuple(hook_ids) for name, hook_ids in listeners.items()}


def record_event(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    event: HookEvent,
    occurred_at: str,
) -> None:
    """Put the event in the inbox of each hook that listens to it.

    Called inside the caller's transaction, so the event commits or rolls back
    with the write it records. An event no hook listens to is not kept.
    """
    hook_ids = _listeners().get(event.name, ())
    if not hook_ids:
        return
    payload_json = json.dumps(event.payload(), ensure_ascii=False, sort_keys=True)
    connection.executemany(
        """INSERT INTO hook_inbox(hook_id, user_id, event_name, payload_json, occurred_at)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (hook_id, user_id, event.name, payload_json, occurred_at)
            for hook_id in hook_ids
        ],
    )


def consume_in_connection(
    connection: sqlite3.Connection, *, hook_id: str, delivery_ids: Collection[int]
) -> None:
    """Remove handed or dropped events, in the caller's transaction."""
    connection.executemany(
        "DELETE FROM hook_inbox WHERE hook_id = ? AND delivery_id = ?",
        [(hook_id, delivery_id) for delivery_id in delivery_ids],
    )


class SqliteHookInbox:
    def __init__(self, *, db_path: Path, busy_timeout_ms: int, user_id: str) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        self._user_id = user_id

    def _connect(self) -> closing[sqlite3.Connection]:
        connection = sqlite3.connect(self._db_path)
        configure_connection(connection, self._busy_timeout_ms)
        return closing(connection)

    def read(self, hook_id: str) -> Sequence[InboxEvent]:
        # Design limit: a tick reads a hook's whole inbox; an inbox stays a few
        # hundred rows deep while its run is active. Page the read if a tick of
        # the hooks task exceeds a second.
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT delivery_id, event_name, payload_json, occurred_at
                   FROM hook_inbox WHERE user_id = ? AND hook_id = ?
                   ORDER BY delivery_id""",
                (self._user_id, hook_id),
            ).fetchall()
        return tuple(
            InboxEvent(
                delivery_id=int(delivery_id),
                name=str(name),
                payload=_payload(str(payload_json)),
                occurred_at=parse_iso_z(str(occurred_at)),
            )
            for delivery_id, name, payload_json, occurred_at in rows
        )

    def drop(self, hook_id: str, delivery_ids: Collection[int]) -> None:
        with self._connect() as connection, connection:
            consume_in_connection(
                connection, hook_id=hook_id, delivery_ids=delivery_ids
            )


def _payload(payload_json: str) -> Mapping[str, EventValue]:
    payload: dict[str, EventValue] = json.loads(payload_json)
    return payload

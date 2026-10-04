"""Seed the runtime rows the WebSocket Suggestion relay attaches to.

The runtime worker owns Suggestion creation, so a WS test that needs a live
Suggestion process writes the same rows the worker's enqueue would commit.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.users import ensure_user_row

RELAYED_SUGGESTION_ANSWER = "小さくPRを切って早くレビューを回しましょう。"


class SeededSuggestion(NamedTuple):
    process_id: str
    suggestion_id: str


def seed_live_suggestion_process(*, user_id: str) -> SeededSuggestion:
    """Insert the enqueued suggestion process a runtime worker would create."""
    process_id = str(uuid.uuid4())
    suggestion_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        ensure_user_row(connection, user_id=user_id, timestamp=now)
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, suggestion_id,
                started_at, updated_at, heartbeat_at, next_event_seq
            ) VALUES (?, ?, 'suggestion', 'enqueued', ?, ?, ?, ?, 1)
            """,
            (process_id, user_id, suggestion_id, now, now, now),
        )
    return SeededSuggestion(process_id=process_id, suggestion_id=suggestion_id)


def seed_terminal_suggestion_row(
    repository: Any,
    *,
    user_id: str,
    suggestion_id: str,
    answer: str = RELAYED_SUGGESTION_ANSWER,
) -> None:
    """Store the terminal Suggestion row as it is once released to be shown."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    repository.data.setdefault("agent_suggestions", []).append(
        {
            "suggestion_id": suggestion_id,
            "user_id": user_id,
            "status": "success",
            "answer": answer,
            "has_suggestion": True,
            "interaction_contract": "action_offer",
            "delivery_state": "released",
            "user_reaction": None,
            "action_status": None,
            "action_command_id": None,
            "created_at": now,
            "updated_at": now,
        }
    )

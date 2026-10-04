"""The logged-out owner: the installation-scoped owner of local data."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.storage.users import ensure_user_row

from .utc_timestamps import now_utc_iso

LOCAL_OWNER_ROW_ID = 1


def ensure_logged_out_owner(*, db_path: Path, busy_timeout_ms: int) -> str:
    """Return the logged-out owner id, creating it on the first start of a store."""
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        with immediate_transaction(connection):
            row = connection.execute(
                "SELECT user_id FROM local_owner WHERE id = ?",
                (LOCAL_OWNER_ROW_ID,),
            ).fetchone()
            if row is not None:
                return str(row[0])
            user_id = str(uuid.uuid4())
            created_at = now_utc_iso()
            ensure_user_row(connection, user_id=user_id, timestamp=created_at)
            connection.execute(
                "INSERT INTO local_owner(id, user_id, created_at) VALUES (?, ?, ?)",
                (LOCAL_OWNER_ROW_ID, user_id, created_at),
            )
            return user_id


__all__ = ["LOCAL_OWNER_ROW_ID", "ensure_logged_out_owner"]

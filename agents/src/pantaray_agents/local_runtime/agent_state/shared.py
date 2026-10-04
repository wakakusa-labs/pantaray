from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.repositories.repository import DBRow


class LocalAgentStateRepositoryBase:
    def __init__(self, *, db_path: Path | str, busy_timeout_ms: int) -> None:
        if busy_timeout_ms <= 0:
            raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")
        self._db_path = str(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        configure_connection(connection, self._busy_timeout_ms)
        connection.row_factory = sqlite3.Row
        return connection


def decode_json_column(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MigrationError("JSON column must be stored as TEXT")
    if not value.strip():
        return None
    return json.loads(value)


def encode_json_column(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_audit_timestamps(*, created_at: str | None = None) -> tuple[str, str]:
    updated_at = now_utc_iso()
    return created_at or updated_at, updated_at


def normalize_row(row: sqlite3.Row, *, json_columns: set[str]) -> DBRow:
    normalized: DBRow = {}
    for key in row.keys():
        raw_value = row[key]
        if key in json_columns:
            normalized[key] = decode_json_column(raw_value)
            continue
        normalized[key] = raw_value
    return normalized

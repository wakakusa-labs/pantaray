from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult
from pantaray_agents.utils.memory_source_policy import build_short_lookback_range

from .shared import normalize_row

_ACTION_JSON_COLUMNS = {"error"}


class _ActionRepositoryContextState(Protocol):
    _db_path: str | Path
    _busy_timeout_ms: int

    def _connect(self) -> sqlite3.Connection: ...


class LocalActionRepositoryContextMixin:
    async def get_recent_short_term_insights(
        self: _ActionRepositoryContextState,
        user_id: str,
        *,
        since_iso: str | None = None,
        limit: int = 5,
    ) -> RepositoryResult[list[DBRow]]:
        cutoff = since_iso or build_short_lookback_range(datetime.now(UTC))[0]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    insight_id,
                    user_id,
                    short_term_insight_data,
                    created_at,
                    updated_at
                FROM agent_insights
                WHERE user_id = ?
                  AND short_term_insight_data IS NOT NULL
                  AND TRIM(short_term_insight_data) != ''
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, cutoff, limit),
            ).fetchall()
        return RepositoryResult(
            data=[normalize_row(row, json_columns=_ACTION_JSON_COLUMNS) for row in rows]
        )


__all__ = ["LocalActionRepositoryContextMixin"]

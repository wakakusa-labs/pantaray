from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryNode,
    MemoryRevision,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    load_latest_active_node_by_source,
    load_revision,
)
from pantaray_agents.repositories.action_support.initial_memory_context_contract import (
    InitialFactsBrief,
    InitialInsightBrief,
    InitialMemoryContext,
)
from pantaray_agents.schema.repositories.repository import DBRow, RepositoryResult
from pantaray_agents.utils.memory_source_policy import build_short_lookback_range

from .shared import normalize_row

_ACTION_JSON_COLUMNS = {"error"}


@dataclass(frozen=True, slots=True)
class _MemoryHead:
    node: MemoryNode
    revision: MemoryRevision

    @property
    def profile_brief(self) -> str:
        return (self.revision.profile_brief or "").strip()


class _ActionRepositoryContextState(Protocol):
    _db_path: str | Path
    _busy_timeout_ms: int

    def _connect(self) -> sqlite3.Connection: ...


class LocalActionRepositoryContextMixin:
    async def get_initial_memory_context(
        self: _ActionRepositoryContextState,
        user_id: str,
    ) -> RepositoryResult[InitialMemoryContext]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            insight_head = _load_memory_head(
                connection, user_id=user_id, source="long_term_insight"
            )
            facts_head = _load_memory_head(connection, user_id=user_id, source="fact")
            connection.commit()

        insight = (
            InitialInsightBrief(
                insight_id=insight_head.node.source_record_id,
                insight_profile_brief=insight_head.profile_brief,
                created_at=insight_head.revision.created_at,
                updated_at=insight_head.revision.created_at,
            )
            if insight_head is not None and insight_head.profile_brief
            else None
        )
        facts = (
            InitialFactsBrief(
                fact_id=facts_head.node.source_record_id,
                facts_profile_brief=facts_head.profile_brief,
                created_at=facts_head.revision.created_at,
                updated_at=facts_head.revision.created_at,
            )
            if facts_head is not None and facts_head.profile_brief
            else None
        )
        return RepositoryResult(
            data=InitialMemoryContext(
                insight=insight,
                facts=facts,
            )
        )

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


def _load_memory_head(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    source: MemorySource,
) -> _MemoryHead | None:
    node = load_latest_active_node_by_source(
        connection=connection,
        user_id=user_id,
        source=source,
    )
    if node is None or node.current_revision_id is None:
        return None
    revision = load_revision(
        connection=connection,
        user_id=user_id,
        revision_id=node.current_revision_id,
    )
    if revision is None:
        raise MemoryCatalogIntegrityError(
            f"Memory Catalog revision missing for {source} head {node.node_id}."
        )
    return _MemoryHead(node=node, revision=revision)

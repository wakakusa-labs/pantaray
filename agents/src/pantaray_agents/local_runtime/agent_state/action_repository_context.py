from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryCatalogIntegrityError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryNode,
    MemoryRevision,
    MemorySource,
)
from pantaray_agents.local_runtime.memory_catalog.record_context import (
    VisibleMemoryRecord,
    build_record_context_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.repository import (
    load_latest_active_node_by_source,
    load_revision,
)
from pantaray_agents.repositories.action_support.initial_memory_context_contract import (
    InitialFactsBrief,
    InitialInsightBrief,
    InitialMemoryArtifact,
    InitialMemoryArtifactFile,
    InitialMemoryContext,
    InitialMemorySourceType,
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
        *,
        action_id: str,
        suggestion_id: str | None,
    ) -> RepositoryResult[InitialMemoryContext]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            insight_head = _load_memory_head(
                connection, user_id=user_id, source="long_term_insight"
            )
            facts_head = _load_memory_head(connection, user_id=user_id, source="fact")
            experience_head = _load_memory_head(
                connection, user_id=user_id, source="agent_experience"
            )
            heads: tuple[
                tuple[MemorySource, InitialMemorySourceType, str, _MemoryHead | None],
                ...,
            ] = (
                (
                    "long_term_insight",
                    "long_term_insight",
                    "current long-term insight",
                    insight_head,
                ),
                ("fact", "facts", "current structured facts", facts_head),
                (
                    "agent_experience",
                    "agent_experience",
                    "current agent experience",
                    experience_head,
                ),
            )
            context_records: list[VisibleMemoryRecord] = []
            if suggestion_id is not None:
                context_records.append(
                    VisibleMemoryRecord(
                        "suggestion", suggestion_id, "accepted suggestion"
                    )
                )
            artifacts: list[InitialMemoryArtifact] = []
            for catalog_source, source_type, label, head in heads:
                if head is None:
                    continue
                artifacts.append(
                    _build_memory_artifact(
                        connection,
                        user_id=user_id,
                        source_type=source_type,
                        head=head,
                    )
                )
                context_records.append(
                    VisibleMemoryRecord(
                        catalog_source, head.node.source_record_id, label
                    )
                )
            context_epoch = build_record_context_epoch(
                connection=connection,
                user_id=user_id,
                run_id=action_id,
                records=tuple(context_records),
            )
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
                artifacts=tuple(artifacts),
                context_epoch=serialize_memory_epoch(context_epoch),
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


def _build_memory_artifact(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    source_type: InitialMemorySourceType,
    head: _MemoryHead,
) -> InitialMemoryArtifact:
    revision = head.revision
    if revision.artifact_root_path is None:
        raise MemoryCatalogIntegrityError(
            f"Memory Catalog {source_type} head {revision.revision_id} is not an artifact."
        )
    files = connection.execute(
        """
        SELECT source_path, content_sha256, content_text
        FROM memory_fragments
        WHERE user_id = ? AND revision_id = ? AND block_kind = 'document_root'
        ORDER BY source_path
        """,
        (user_id, revision.revision_id),
    ).fetchall()
    if not files:
        raise MemoryCatalogIntegrityError(
            "Memory Catalog artifact revision has no document roots for "
            f"{revision.revision_id}."
        )
    root_path = revision.artifact_root_path.strip().strip("/")
    return InitialMemoryArtifact(
        source_type=source_type,
        source_record_id=head.node.source_record_id,
        artifact_id=revision.revision_id,
        logical_updated_at=revision.created_at,
        files=tuple(
            InitialMemoryArtifactFile(
                storage_path="/".join((root_path, str(row["source_path"]))),
                sha256=str(row["content_sha256"]),
                byte_size=len(str(row["content_text"]).encode("utf-8")),
                mime_type="text/markdown",
            )
            for row in files
        ),
    )

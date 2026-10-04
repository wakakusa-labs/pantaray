from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.repositories.repository import (
    RepositoryErrorKind,
    RepositoryResult,
)
from pantaray_agents.utils.memory_source_policy import (
    MEMORY_SOURCE_ORDER,
    MemorySourceCoverageSlot,
    MemorySourceCoverageSnapshot,
    MemorySourceId,
    MemorySourceStatus,
    build_activity_description_window,
    build_short_lookback_range,
)


class _ConnectableRepository(Protocol):
    def _connect(self) -> sqlite3.Connection: ...


@dataclass(frozen=True, slots=True)
class _CoverageResolution:
    status: MemorySourceStatus
    latest_at: str | None


class _CoverageInvariantError(ValueError):
    def __init__(self, *, source: MemorySourceId, detail: str) -> None:
        super().__init__(
            "get_memory_source_coverage_snapshot: "
            f"{source} coverage invariant violated: {detail}"
        )


class LocalActionRepositoryMemoryMixin:
    async def get_memory_source_coverage_snapshot(
        self,
        *,
        user_id: str,
        suggestion_created_at: str | None,
        max_parallel_queries: int,
    ) -> RepositoryResult[MemorySourceCoverageSnapshot]:
        _ = max_parallel_queries
        if not user_id:
            return RepositoryResult(
                error="get_memory_source_coverage_snapshot: user_id is required"
            )
        now = datetime.now(UTC)
        short_from, short_to = build_short_lookback_range(now)
        activity_window: tuple[str, str] | None = None
        if isinstance(suggestion_created_at, str) and suggestion_created_at.strip():
            try:
                activity_window = build_activity_description_window(
                    suggestion_created_at.strip()
                )
            except ValueError:
                activity_window = None
        repository = cast(_ConnectableRepository, self)
        try:
            with repository._connect() as connection:
                slots = [
                    self._build_coverage_slot(
                        connection=connection,
                        source=source,
                        user_id=user_id,
                        short_from=short_from,
                        short_to=short_to,
                        activity_window=activity_window,
                    )
                    for source in MEMORY_SOURCE_ORDER
                ]
        except _CoverageInvariantError as exc:
            return RepositoryResult(
                error=str(exc),
                error_kind=RepositoryErrorKind.CONSTRAINT,
                retryable=False,
            )
        return RepositoryResult(
            data={
                "evaluated_at": format_utc_iso(now),
                "slots": slots,
            }
        )

    def _build_coverage_slot(
        self,
        *,
        connection: sqlite3.Connection,
        source: MemorySourceId,
        user_id: str,
        short_from: str,
        short_to: str,
        activity_window: tuple[str, str] | None,
    ) -> MemorySourceCoverageSlot:
        resolution = self._resolve_coverage_slot(
            connection=connection,
            source=source,
            user_id=user_id,
            short_from=short_from,
            short_to=short_to,
            activity_window=activity_window,
        )
        return {
            "source": source,
            "status": resolution.status,
            "latest_at": resolution.latest_at,
        }

    def _resolve_coverage_slot(
        self,
        *,
        connection: sqlite3.Connection,
        source: MemorySourceId,
        user_id: str,
        short_from: str,
        short_to: str,
        activity_window: tuple[str, str] | None,
    ) -> _CoverageResolution:
        if source in {"long_term_insight", "facts"}:
            return self._resolve_artifact_source_slot(
                connection=connection,
                user_id=user_id,
                source=source,
            )
        if source == "short_term_insight":
            row = connection.execute(
                """
                SELECT created_at, updated_at
                FROM agent_insights
                WHERE user_id = ?
                  AND status = ?
                  AND short_term_insight_data IS NOT NULL
                  AND TRIM(short_term_insight_data) != ''
                  AND created_at >= ?
                  AND created_at <= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, StatusType.SUCCESS.value, short_from, short_to),
            ).fetchone()
            return self._build_coverage_resolution_from_row(
                row,
                "created_at",
                "updated_at",
            )
        if source == "suggestions":
            row = connection.execute(
                """
                SELECT created_at, updated_at
                FROM agent_suggestions
                WHERE user_id = ?
                  AND status != ?
                  AND created_at >= ?
                  AND created_at <= ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, StatusType.PROCESSING.value, short_from, short_to),
            ).fetchone()
            return self._build_coverage_resolution_from_row(
                row,
                "created_at",
                "updated_at",
            )
        if source == "actions":
            row = connection.execute(
                """
                SELECT updated_at, created_at
                FROM agent_actions
                WHERE user_id = ?
                  AND status != ?
                  AND updated_at >= ?
                  AND updated_at <= ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, StatusType.PROCESSING.value, short_from, short_to),
            ).fetchone()
            return self._build_coverage_resolution_from_row(
                row,
                "updated_at",
                "created_at",
            )
        if source == "activity_summary":
            row = connection.execute(
                """
                SELECT period_end, created_at
                FROM activity_summaries
                WHERE user_id = ?
                  AND status = ?
                  AND summary IS NOT NULL
                  AND TRIM(summary) != ''
                ORDER BY period_end DESC
                LIMIT 1
                """,
                (user_id, StatusType.SUCCESS.value),
            ).fetchone()
            return self._build_coverage_resolution_from_row(
                row,
                "period_end",
                "created_at",
            )
        if source == "activity_description":
            if activity_window is None:
                return _CoverageResolution(status="unknown", latest_at=None)
            row = connection.execute(
                """
                SELECT period_end
                FROM activity_logs
                WHERE user_id = ?
                  AND status = ?
                  AND description IS NOT NULL
                  AND TRIM(description) != ''
                  AND period_end >= ?
                  AND period_end <= ?
                ORDER BY period_end DESC
                LIMIT 1
                """,
                (
                    user_id,
                    StatusType.SUCCESS.value,
                    activity_window[0],
                    activity_window[1],
                ),
            ).fetchone()
            return self._build_coverage_resolution_from_row(row, "period_end")
        return _CoverageResolution(status="unknown", latest_at=None)

    def _resolve_artifact_source_slot(
        self,
        *,
        connection: sqlite3.Connection,
        user_id: str,
        source: MemorySourceId,
    ) -> _CoverageResolution:
        catalog_source = "fact" if source == "facts" else source
        row = connection.execute(
            """
            SELECT nodes.updated_at, revisions.body_kind,
                   revisions.content_sha256,
                   EXISTS (
                       SELECT 1 FROM memory_fragments AS fragments
                       WHERE fragments.user_id = revisions.user_id
                         AND fragments.revision_id = revisions.revision_id
                         AND fragments.block_kind = 'document_root'
                   ) AS has_document
            FROM memory_nodes AS nodes
            JOIN memory_revisions AS revisions
              ON revisions.user_id = nodes.user_id
             AND revisions.revision_id = nodes.current_revision_id
            WHERE nodes.user_id = ? AND nodes.source_type = ?
              AND nodes.lifecycle = 'active' AND nodes.integrity = 'healthy'
            ORDER BY nodes.updated_at DESC
            LIMIT 1
            """,
            (user_id, catalog_source),
        ).fetchone()
        if row is None:
            return _CoverageResolution(status="missing", latest_at=None)
        if (
            row["body_kind"] != "artifact_tree"
            or not isinstance(row["content_sha256"], str)
            or not str(row["content_sha256"]).strip()
            or int(row["has_document"]) != 1
        ):
            raise _CoverageInvariantError(
                source=source,
                detail="current Catalog artifact revision is incomplete",
            )
        updated_at = row["updated_at"]
        if not isinstance(updated_at, str) or not updated_at.strip():
            raise _CoverageInvariantError(
                source=source,
                detail="current Catalog node is missing updated_at",
            )
        return _CoverageResolution(status="present", latest_at=updated_at)

    @staticmethod
    def _build_coverage_resolution_from_row(
        row: sqlite3.Row | None,
        *fields: str,
    ) -> _CoverageResolution:
        if row is None:
            return _CoverageResolution(status="missing", latest_at=None)
        for field in fields:
            value = row[field]
            if isinstance(value, str) and value.strip():
                return _CoverageResolution(status="present", latest_at=value)
        return _CoverageResolution(status="missing", latest_at=None)

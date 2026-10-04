"""MockActionAgentRepository の memory 系責務。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.services.memory_search.source_config import (
    ROW_SEARCH_CONFIG,
    text_source_status_matches,
)
from pantaray_agents.local_runtime.memory_references import build_memory_key
from pantaray_agents.local_runtime.memory_references.reference_ids import (
    build_activity_reference_ids_for_targets,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.schema.agent.base import StatusType
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    RepositoryErrorKind,
    RepositoryResult,
)
from pantaray_agents.utils.memory_source_policy import (
    EMPTY_TEXT_SHA256_HEX,
    MEMORY_SOURCE_ORDER,
    MemorySourceCoverageSlot,
    MemorySourceCoverageSnapshot,
    MemorySourceId,
    MemorySourceStatus,
    build_activity_description_window,
    build_short_lookback_range,
)
from pantaray_agents.utils.strict_numbers import is_strict_int
from pantaray_agents.utils.timestamps import parse_iso8601_utc

from ..mock_action_agent_repository_types import (
    CoverageRow,
    CoverageRowValue,
    MemorySearchResultRow,
    SourceOptionsPayload,
)
from .artifact_search import search_mock_artifact_source_rows
from .memory_search import (
    memory_search_sources_for_focus,
    mock_memory_key_for_source,
    mock_memory_search_keywords,
    mock_memory_search_snippet,
    mock_memory_search_text_score,
    mock_time_hint_score,
    parse_mock_datetime,
    validate_mock_memory_search_request,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..mock_action_agent_repository import MockActionAgentRepository


@dataclass(frozen=True, slots=True)
class _MockCoverageResolution:
    status: MemorySourceStatus
    latest_at: str | None


class _MockCoverageInvariantError(ValueError):
    def __init__(self, *, source: MemorySourceId, detail: str) -> None:
        super().__init__(
            "get_memory_source_coverage_snapshot: "
            f"{source} coverage invariant violated: {detail}"
        )


class MockActionAgentMemoryMixin:
    async def get_memory_source_coverage_snapshot(
        self: MockActionAgentRepository,
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
        now_dt = datetime.now(UTC)
        short_from, short_to = build_short_lookback_range(now_dt)
        short_from_dt = parse_iso8601_utc(short_from)
        short_to_dt = parse_iso8601_utc(short_to)
        activity_window: tuple[str, str] | None = None
        if isinstance(suggestion_created_at, str) and suggestion_created_at.strip():
            try:
                activity_window = build_activity_description_window(
                    suggestion_created_at.strip()
                )
            except ValueError:
                activity_window = None

        def _to_utc_dt(value: CoverageRowValue | None) -> datetime | None:
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    return None
                return value.astimezone(UTC)
            if not isinstance(value, str) or not value:
                return None
            try:
                return parse_iso8601_utc(value)
            except ValueError:
                return None

        def _status_text(value: CoverageRowValue | None) -> str:
            if isinstance(value, StatusType):
                return value.value
            if isinstance(value, str):
                return value.strip()
            return ""

        def _is_non_blank_text(value: CoverageRowValue | None) -> bool:
            return isinstance(value, str) and bool(value.strip())

        def _within_short_window(value: CoverageRowValue | None) -> bool:
            dt = _to_utc_dt(value)
            return dt is not None and short_from_dt <= dt <= short_to_dt

        def _pick_latest_row(
            rows: list[CoverageRow],
            *candidates: str,
        ) -> CoverageRow | None:
            latest_row: CoverageRow | None = None
            latest_dt: datetime | None = None
            for row in rows:
                for field in candidates:
                    dt = _to_utc_dt(row.get(field))
                    if dt is None:
                        continue
                    if latest_dt is None or dt > latest_dt:
                        latest_dt = dt
                        latest_row = row
                    break
            return latest_row

        def _pick_latest(
            rows: list[CoverageRow],
            *candidates: str,
        ) -> _MockCoverageResolution:
            latest_row = _pick_latest_row(rows, *candidates)
            if latest_row is None:
                return _MockCoverageResolution(status="missing", latest_at=None)
            for field in candidates:
                raw = latest_row.get(field)
                dt = _to_utc_dt(raw)
                if dt is None:
                    continue
                if isinstance(raw, str) and raw:
                    return _MockCoverageResolution(status="present", latest_at=raw)
                return _MockCoverageResolution(
                    status="present",
                    latest_at=dt.isoformat(),
                )
            return _MockCoverageResolution(status="missing", latest_at=None)

        def _require_latest_at(
            *,
            row: CoverageRow,
            source: MemorySourceId,
            detail: str,
            fields: tuple[str, ...],
        ) -> str:
            resolution = _pick_latest([row], *fields)
            if resolution.status == "present" and resolution.latest_at is not None:
                return resolution.latest_at
            raise _MockCoverageInvariantError(source=source, detail=detail)

        def _resolve_artifact_slot(source: MemorySourceId) -> _MockCoverageResolution:
            artifact_rows = [
                row
                for row in self.data.get("memory_artifacts", [])
                if row.get("user_id") == user_id
                and row.get("source_type") == source
                and _is_non_blank_text(row.get("content_sha256"))
                and str(row.get("content_sha256")).strip() != EMPTY_TEXT_SHA256_HEX
            ]
            latest_artifact = _pick_latest_row(
                artifact_rows,
                "logical_updated_at",
                "logical_created_at",
            )
            if latest_artifact is None:
                return _MockCoverageResolution(status="missing", latest_at=None)

            latest_at = _require_latest_at(
                row=latest_artifact,
                source=source,
                detail="artifact metadata row is missing logical timestamps",
                fields=("logical_updated_at", "logical_created_at"),
            )
            artifact_id = latest_artifact.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise _MockCoverageInvariantError(
                    source=source,
                    detail="artifact metadata row is missing artifact_id",
                )

            file_ids = {
                str(file_row.get("file_id"))
                for file_row in self.data.get("memory_artifact_files", [])
                if file_row.get("artifact_id") == artifact_id
                and isinstance(file_row.get("file_id"), str)
                and str(file_row.get("file_id")).strip()
            }
            has_blocks = any(
                block_row.get("file_id") in file_ids
                for block_row in self.data.get("memory_artifact_blocks", [])
            )
            if not file_ids or not has_blocks:
                raise _MockCoverageInvariantError(
                    source=source,
                    detail=(
                        "artifact metadata exists but searchable file/block "
                        f"projection is missing (artifact_id={artifact_id}, "
                        f"latest_at={latest_at})"
                    ),
                )
            return _MockCoverageResolution(status="present", latest_at=latest_at)

        try:
            slots: list[MemorySourceCoverageSlot] = []
            for source in MEMORY_SOURCE_ORDER:
                if source in {"long_term_insight", "facts"}:
                    resolution = _resolve_artifact_slot(source)
                elif source == "short_term_insight":
                    insight_rows = [
                        row
                        for row in self.data.get("insights", [])
                        if row.get("user_id") == user_id
                        and _status_text(row.get("status")) == StatusType.SUCCESS.value
                        and _is_non_blank_text(row.get("short_term_insight_data"))
                        and _within_short_window(row.get("created_at"))
                    ]
                    resolution = _pick_latest(
                        insight_rows,
                        "created_at",
                        "updated_at",
                    )
                elif source == "suggestions":
                    suggestion_rows = [
                        row
                        for row in self.data.get("suggestions", [])
                        if row.get("user_id") == user_id
                        and bool(_status_text(row.get("status")))
                        and _status_text(row.get("status"))
                        != StatusType.PROCESSING.value
                        and _within_short_window(row.get("created_at"))
                    ]
                    resolution = _pick_latest(
                        suggestion_rows,
                        "created_at",
                        "updated_at",
                    )
                elif source == "actions":
                    action_rows = [
                        row
                        for row in self.data.get("actions", [])
                        if row.get("user_id") == user_id
                        and bool(_status_text(row.get("status")))
                        and _status_text(row.get("status"))
                        != StatusType.PROCESSING.value
                        and _within_short_window(
                            row.get("updated_at") or row.get("created_at")
                        )
                    ]
                    resolution = _pick_latest(
                        action_rows,
                        "updated_at",
                        "created_at",
                    )
                elif source == "activity_summary":
                    summary_rows = [
                        row
                        for row in self.data.get("activity_summaries", [])
                        if row.get("user_id") == user_id
                        and _status_text(row.get("status")) == StatusType.SUCCESS.value
                        and _is_non_blank_text(row.get("summary"))
                    ]
                    resolution = _pick_latest(
                        summary_rows,
                        "period_end",
                        "created_at",
                    )
                elif source == "activity_description":
                    if activity_window is None:
                        resolution = _MockCoverageResolution(
                            status="unknown",
                            latest_at=None,
                        )
                    else:
                        period_from_dt = parse_iso8601_utc(activity_window[0])
                        period_to_dt = parse_iso8601_utc(activity_window[1])
                        description_rows: list[CoverageRow] = []
                        for row in self.data.get("activity_descriptions", []):
                            if row.get("user_id") != user_id:
                                continue
                            if (
                                _status_text(row.get("status"))
                                != StatusType.SUCCESS.value
                            ):
                                continue
                            if not _is_non_blank_text(row.get("description")):
                                continue
                            candidate_dt = _to_utc_dt(row.get("period_end"))
                            if candidate_dt is None:
                                continue
                            if period_from_dt <= candidate_dt <= period_to_dt:
                                description_rows.append(row)
                        resolution = _pick_latest(description_rows, "period_end")
                else:
                    resolution = _MockCoverageResolution(
                        status="unknown",
                        latest_at=None,
                    )

                slots.append(
                    {
                        "source": source,
                        "status": resolution.status,
                        "latest_at": resolution.latest_at,
                    }
                )
        except _MockCoverageInvariantError as exc:
            return RepositoryResult(
                error=str(exc),
                error_kind=RepositoryErrorKind.CONSTRAINT,
                retryable=False,
            )
        return RepositoryResult(
            data={"evaluated_at": format_utc_iso(now_dt), "slots": slots}
        )

    async def memory_search(
        self: MockActionAgentRepository,
        user_id: str,
        *,
        query: str,
        focus: str = "all",
        time_hint: SourceOptionsPayload | None = None,
        limit: int = 50,
        max_parallel_queries: int | None = None,
    ) -> RepositoryResult[list[MemorySearchResultRow]]:
        _ = max_parallel_queries
        validation_error = validate_mock_memory_search_request(
            query=query,
            focus=focus,
            time_hint=time_hint,
            limit=limit,
        )
        if validation_error is not None:
            return RepositoryResult(
                error=validation_error,
                error_kind=RepositoryErrorKind.VALIDATION,
                retryable=False,
            )

        sources = list(memory_search_sources_for_focus(focus))
        normalized_keywords = mock_memory_search_keywords(query)
        time_hint_center = parse_mock_datetime((time_hint or {}).get("center"))
        time_hint_radius_hours = (time_hint or {}).get("radius_hours")
        normalized_radius_hours = (
            time_hint_radius_hours if is_strict_int(time_hint_radius_hours) else None
        )

        def _sortable_value(value: CoverageRowValue | datetime | None) -> str:
            if isinstance(value, datetime):
                return value.isoformat()
            return str(value or "")

        results: list[MemorySearchResultRow] = []
        for source in sources:
            cfg = ROW_SEARCH_CONFIG.get(source)
            if source in {"long_term_insight", "facts"}:
                results.extend(
                    search_mock_artifact_source_rows(
                        data=self.data,
                        user_id=user_id,
                        source=source,
                        query=query,
                        keywords=normalized_keywords,
                        time_hint_center=time_hint_center,
                        time_hint_radius_hours=normalized_radius_hours,
                        limit=limit,
                    )
                )
                continue
            if cfg is None:
                continue
            if source == "activity_description":
                rows = [
                    row
                    for row in self.data.get("activity_descriptions", [])
                    if row.get("user_id") == user_id
                    and text_source_status_matches(
                        source=source,
                        status=row.get("status"),
                    )
                ]
                for row in rows:
                    description = str(
                        row.get("description") or row.get("content") or ""
                    )
                    if normalized_keywords and not any(
                        keyword in description.casefold()
                        for keyword in normalized_keywords
                    ):
                        continue
                    record_id = str(row.get("log_id") or row.get("record_id") or "")
                    snippet = mock_memory_search_snippet(text=description, query=query)
                    result: MemorySearchResultRow = {
                        "source": "activity_description",
                        "record_id": record_id,
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("period_end") or row.get("updated_at"),
                        "content": snippet,
                        "memory_key": mock_memory_key_for_source(
                            source="activity_description",
                            record_id=record_id,
                        ),
                        "available_ref_ids": [],
                        "period_start": row.get("period_start"),
                        "period_end": row.get("period_end"),
                    }
                    result["score"] = mock_memory_search_text_score(
                        text=description,
                        query=query,
                    ) + mock_time_hint_score(
                        value=row.get("period_end") or row.get("updated_at"),
                        center=time_hint_center,
                        radius_hours=normalized_radius_hours,
                    )
                    results.append(result)
                continue
            if source == "activity_summary":
                if "activity_summaries" not in self.data:
                    continue
                rows = [
                    row
                    for row in self.data["activity_summaries"]
                    if row.get("user_id") == user_id
                    and text_source_status_matches(
                        source=source,
                        status=row.get("status"),
                    )
                ]
                for row in rows:
                    summary = str(row.get("summary", "") or "")
                    if normalized_keywords and not any(
                        keyword in summary.casefold() for keyword in normalized_keywords
                    ):
                        continue
                    snippet = mock_memory_search_snippet(text=summary, query=query)
                    result: MemorySearchResultRow = {
                        "source": "activity_summary",
                        "record_id": str(row.get("summary_id") or ""),
                        "created_at": row.get("created_at"),
                        "updated_at": row.get("period_end"),
                        "content": snippet,
                        "memory_key": mock_memory_key_for_source(
                            source="activity_summary",
                            record_id=str(row.get("summary_id") or ""),
                        ),
                        "available_ref_ids": [
                            *build_activity_reference_ids_for_targets(
                                target_memory_keys=tuple(
                                    build_memory_key(
                                        source=(
                                            "activity_log"
                                            if str(row.get("summary_type") or "")
                                            == "1h"
                                            else "activity_summary"
                                        ),
                                        record_id=str(source_id),
                                    )
                                    for source_id in (row.get("source_ids") or [])
                                    if str(source_id).strip()
                                )
                            )
                        ],
                        "summary_type": row.get("summary_type"),
                        "period_start": row.get("period_start"),
                        "period_end": row.get("period_end"),
                    }
                    result["score"] = mock_memory_search_text_score(
                        text=summary,
                        query=query,
                    ) + mock_time_hint_score(
                        value=row.get("period_end"),
                        center=time_hint_center,
                        radius_hours=normalized_radius_hours,
                    )
                    results.append(result)
                continue

            collection = cfg["table"].removeprefix("agent_")
            if collection not in self.data:
                continue
            rows = sorted(
                self.data[collection],
                key=lambda row: _sortable_value(
                    row.get(cfg["order_column"]) or row.get("updated_at")
                ),
                reverse=True,
            )
            for row in rows:
                if row.get("user_id") != user_id:
                    continue
                if not text_source_status_matches(
                    source=source,
                    status=row.get("status"),
                ):
                    continue
                content_value = str(row.get(cfg["content_field"], "") or "")
                if source == "short_term_insight" and not row.get(
                    "short_term_insight_data"
                ):
                    continue
                if normalized_keywords and not any(
                    keyword in content_value.casefold()
                    for keyword in normalized_keywords
                ):
                    continue
                result = {
                    "source": source,
                    "record_id": row.get(cfg["id_field"]),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "content": mock_memory_search_snippet(
                        text=content_value, query=query
                    ),
                    "available_ref_ids": [],
                    "score": mock_memory_search_text_score(
                        text=content_value,
                        query=query,
                    )
                    + mock_time_hint_score(
                        value=row.get(cfg["time_column"]) or row.get("updated_at"),
                        center=time_hint_center,
                        radius_hours=normalized_radius_hours,
                    ),
                }
                results.append(result)
                memory_key = mock_memory_key_for_source(
                    source=source,
                    record_id=str(row.get(cfg["id_field"]) or ""),
                )
                if memory_key is not None:
                    results[-1]["memory_key"] = memory_key
        results.sort(
            key=lambda row: (
                float(row.get("score") or 0.0),
                _sortable_value(
                    row.get("updated_at")
                    or row.get("period_end")
                    or row.get("created_at")
                ),
            ),
            reverse=True,
        )
        if limit > 0:
            results = results[:limit]
        return RepositoryResult(data=results)

    async def save_suggestion(
        self: MockActionAgentRepository,
        data: DBRow,
    ) -> None:
        await self.save_data("suggestions", data)

    async def save_insight(
        self: MockActionAgentRepository,
        data: DBRow,
    ) -> None:
        await self.save_data("insights", data)

    async def save_structured_fact(
        self: MockActionAgentRepository,
        data: DBRow,
    ) -> None:
        await self.save_data("facts", data)

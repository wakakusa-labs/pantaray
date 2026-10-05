"""MockActionAgentRepository の memory 系責務。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pantaray_agents.agents.action_agent.services.memory_search.source_config import (
    ROW_SEARCH_CONFIG,
    text_source_status_matches,
)
from pantaray_agents.local_runtime.memory_references import build_memory_key
from pantaray_agents.local_runtime.memory_references.reference_ids import (
    build_activity_reference_ids_for_targets,
)
from pantaray_agents.schema.repositories.repository import (
    DBRow,
    RepositoryErrorKind,
    RepositoryResult,
)
from pantaray_agents.utils.strict_numbers import is_strict_int

from ..mock_action_agent_repository_types import (
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


class MockActionAgentMemoryMixin:
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

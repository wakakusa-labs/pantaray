"""SuggestionAgent の context 行型定義。"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

from pantaray_agents.schema.repositories.repository import DBRow, JSONValue

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SuggestionStableMemoryContext:
    """Run開始時に固定されたbounded stable-memory context。"""

    prompt: str
    has_facts: bool
    has_insights: bool
    # The whole `insights/todos.md`, carried in the prompt as is.
    pending_work: str = ""


class ActivityDescriptionRow(TypedDict, total=False):
    """Activity Description の最小利用フィールド。"""

    description: str
    period_start: str
    period_end: str


class ActivitySummaryRow(TypedDict, total=False):
    """Activity Summary の最小利用フィールド。

    Note:
        `source_ids` は ActivitySummaryAgent が要約した下位レコードの id 列。
        空配列は「対象期間に活動がなく定型文だけを書いた」ことを示す構造化された印。
    """

    summary: str
    period_start: str
    period_end: str
    source_ids: list[str]


type SummaryType = Literal["24h", "1w", "1m"]
type ActivitySummaryRowsByType = dict[SummaryType, ActivitySummaryRow | None]


class ContextDensitySlotStatus(TypedDict):
    """Context density 7スロットの present/missing 判定。"""

    activity_descriptions: bool
    activity_summary_1h: bool
    activity_summary_24h: bool
    activity_summary_1w: bool
    activity_summary_1m: bool
    long_term_insight: bool
    long_term_facts: bool


class SuggestionFetchedContext(TypedDict):
    """_fetch_context_data が返す Suggestion 用コンテキスト。"""

    short_term_insight: str
    reconsideration_reason: str
    stable_memory_context: str
    action_agent_capabilities: str
    recent_suggestions: str
    recent_activity_descriptions: str
    recent_activity_summary_1h: str
    recent_activity_summaries_24h_1w_1m: str
    context_density_signal: str
    workspace_context_prompt: str


def _warn_field_treated_as_missing(
    *,
    row_kind: str,
    field: str,
    value: JSONValue,
) -> None:
    """型不正なフィールドを欠損扱いにしたことを記録する。"""

    logger.warning(
        "Suggestion context normalization: invalid field treated as missing",
        extra={
            "row_kind": row_kind,
            "field": field,
            "value_type": type(value).__name__,
        },
    )


def _extract_optional_str(
    *,
    row: Mapping[str, JSONValue],
    field: str,
    row_kind: str,
) -> str | None:
    """context density 用の文字列フィールドを取り出す。

    方針:
    - `None` は欠損として扱う。
    - 非文字列は型不正として警告ログを残し、欠損（missing）として扱う。
    """

    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, str):
        return value

    _warn_field_treated_as_missing(row_kind=row_kind, field=field, value=value)
    return None


def _extract_source_ids(row: Mapping[str, JSONValue]) -> list[str] | None:
    """Activity Summary の `source_ids` を文字列配列として取り出す。

    方針:
    - `None`（未設定）は欠損として扱う。
    - 文字列配列でない値は型不正として警告ログを残し、欠損（= 活動なし）として扱う。
    """

    value = row.get("source_ids")
    if value is None:
        return None
    if isinstance(value, list):
        source_ids = [item for item in value if isinstance(item, str)]
        if len(source_ids) == len(value):
            return source_ids

    _warn_field_treated_as_missing(
        row_kind="activity_summary",
        field="source_ids",
        value=value,
    )
    return None


def normalize_activity_description_row(
    row: Mapping[str, JSONValue],
) -> ActivityDescriptionRow:
    """Repository 行を ActivityDescriptionRow へ正規化する。

    Note:
        この正規化は context density 判定用。型不正な値は missing 扱いに寄せる。
    """

    normalized: ActivityDescriptionRow = {}
    description = _extract_optional_str(
        row=row,
        field="description",
        row_kind="activity_description",
    )
    period_start = _extract_optional_str(
        row=row,
        field="period_start",
        row_kind="activity_description",
    )
    period_end = _extract_optional_str(
        row=row,
        field="period_end",
        row_kind="activity_description",
    )
    if description is not None:
        normalized["description"] = description
    if period_start is not None:
        normalized["period_start"] = period_start
    if period_end is not None:
        normalized["period_end"] = period_end
    return normalized


def normalize_activity_summary_row(row: Mapping[str, JSONValue]) -> ActivitySummaryRow:
    """Repository 行を ActivitySummaryRow へ正規化する。

    Note:
        この正規化は context density 判定用。型不正な値は missing 扱いに寄せる。
    """

    normalized: ActivitySummaryRow = {}
    summary = _extract_optional_str(
        row=row,
        field="summary",
        row_kind="activity_summary",
    )
    period_start = _extract_optional_str(
        row=row,
        field="period_start",
        row_kind="activity_summary",
    )
    period_end = _extract_optional_str(
        row=row,
        field="period_end",
        row_kind="activity_summary",
    )
    source_ids = _extract_source_ids(row)
    if summary is not None:
        normalized["summary"] = summary
    if period_start is not None:
        normalized["period_start"] = period_start
    if period_end is not None:
        normalized["period_end"] = period_end
    if source_ids is not None:
        normalized["source_ids"] = source_ids
    return normalized


def normalize_activity_description_rows(
    rows: Sequence[DBRow] | None,
) -> list[ActivityDescriptionRow]:
    """Activity Description 行配列を正規化する。

    Note:
        行全体が Mapping でない場合は内部不整合として例外送出する。
    """

    if not rows:
        return []

    normalized: list[ActivityDescriptionRow] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(
                "ActivityDescription row must be a mapping "
                f"(index={index}, type={type(row).__name__})"
            )
        normalized.append(normalize_activity_description_row(row))
    return normalized


def normalize_activity_summary_rows(
    rows: Sequence[DBRow] | None,
) -> list[ActivitySummaryRow]:
    """Activity Summary 行配列を正規化する。

    Note:
        行全体が Mapping でない場合は内部不整合として例外送出する。
    """

    if not rows:
        return []

    normalized: list[ActivitySummaryRow] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(
                "ActivitySummary row must be a mapping "
                f"(index={index}, type={type(row).__name__})"
            )
        normalized.append(normalize_activity_summary_row(row))
    return normalized

"""SuggestionAgent の context density 計算ヘルパー。"""

from collections.abc import Sequence
from datetime import UTC, datetime

from .context_types import (
    ActivityDescriptionRow,
    ActivitySummaryRow,
    ContextDensitySlotStatus,
)

CONTEXT_DENSITY_SLOT_ORDER: tuple[tuple[str, str], ...] = (
    ("activity_descriptions", "Activity Description (recent 3)"),
    ("activity_summary_1h", "Activity Summary (1h)"),
    ("activity_summary_24h", "Activity Summary (24h)"),
    ("long_term_insight", "Long-term Insight"),
    ("long_term_facts", "Long-term Facts"),
)
CONTEXT_DENSITY_LOW_MAX_PRESENT = 2
CONTEXT_DENSITY_MEDIUM_MAX_PRESENT = 4


def parse_iso_datetime(value: str | datetime | None) -> datetime | None:
    """ISO 8601 文字列（Z / +00:00 を含む）をUTCのdatetimeへ変換する（失敗時はNone）。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_activity_description_present(
    rows: Sequence[ActivityDescriptionRow] | None,
) -> bool:
    """Activity Description スロットの present 判定を返す。"""
    if not rows:
        return False

    for row in rows:
        description = str(row.get("description") or "").strip()
        period_end = parse_iso_datetime(row.get("period_end"))
        if description and period_end is not None:
            return True
    return False


def is_activity_summary_row_present(row: ActivitySummaryRow | None) -> bool:
    """Activity Summary 1スロット分（1h/24h）の present 判定を返す。

    `source_ids` が空の行は ActivitySummaryAgent が下位レコードを1件も要約できず
    「活動なし」定型文だけを書いた行なので、参照可能なコンテキストとしては missing。
    """
    if not row or not row.get("source_ids"):
        return False

    summary = str(row.get("summary") or "").strip()
    period_end = parse_iso_datetime(row.get("period_end"))
    return bool(summary) and period_end is not None


def build_context_density_slot_status(
    *,
    has_long_term_insight: bool,
    has_long_term_facts: bool,
    activity_desc_rows: Sequence[ActivityDescriptionRow] | None,
    summary_1h_row: ActivitySummaryRow | None,
    summary_24h_row: ActivitySummaryRow | None,
) -> ContextDensitySlotStatus:
    """5スロット分の present/missing 判定を構築する。"""
    return {
        "activity_descriptions": is_activity_description_present(activity_desc_rows),
        "activity_summary_1h": is_activity_summary_row_present(summary_1h_row),
        "activity_summary_24h": is_activity_summary_row_present(summary_24h_row),
        "long_term_insight": has_long_term_insight,
        "long_term_facts": has_long_term_facts,
    }


def classify_context_density(present_count: int) -> str:
    """present スロット数からコンテキスト密度を low/medium/high に分類する。"""
    slot_count = len(CONTEXT_DENSITY_SLOT_ORDER)
    if present_count < 0 or present_count > slot_count:
        raise ValueError(
            f"present_count must be within 0..{slot_count}: {present_count}"
        )
    if present_count <= CONTEXT_DENSITY_LOW_MAX_PRESENT:
        return "low"
    if present_count <= CONTEXT_DENSITY_MEDIUM_MAX_PRESENT:
        return "medium"
    return "high"


def build_context_density_signal(
    *,
    density: str,
    present_count: int,
    slot_status: ContextDensitySlotStatus,
) -> str:
    """プロンプト投入用の Context Density Signal テキストを構築する。"""
    expected_slot_keys = {slot_key for slot_key, _ in CONTEXT_DENSITY_SLOT_ORDER}
    actual_slot_keys = set(slot_status.keys())
    if actual_slot_keys != expected_slot_keys:
        missing_keys = sorted(expected_slot_keys - actual_slot_keys)
        unexpected_keys = sorted(actual_slot_keys - expected_slot_keys)
        raise ValueError(
            "slot_status keys mismatch: "
            f"missing={missing_keys}, unexpected={unexpected_keys}"
        )

    lines = [
        f"context_density: {density}",
        f"present_count: {present_count}/{len(CONTEXT_DENSITY_SLOT_ORDER)}",
        "slot_status:",
    ]
    for slot_key, label in CONTEXT_DENSITY_SLOT_ORDER:
        status = "present" if slot_status[slot_key] else "missing"
        lines.append(f"- {label}: {status}")
    return "\n".join(lines)

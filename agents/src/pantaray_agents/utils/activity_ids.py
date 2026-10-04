"""Activity系エージェントのID生成ユーティリティ。

目的:
    - ActivitySummary の `summary_id` を決定的（UUIDv5）に生成し、冪等性を担保する。
    - `period_start` の表記ゆれ（"Z" / "+00:00" など）を UTC/Z 形式へ正規化して吸収する。

注意:
    - このモジュールは「ID生成の決定式」をSSOTとして集中させ、スケジューラ/エージェント間で不一致が
      起きないようにする。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

# ActivitySummary の summary_id（UUIDv5）生成に用いる固定NAMESPACE。
#
# 由来:
#     uuidv5(uuid.NAMESPACE_URL, "pantaray:activity_summary:v1")
NAMESPACE_ACTIVITY_SUMMARY = uuid.UUID("2dbf8c64-d862-5cf7-9442-dc63cf598668")


def normalize_iso_to_iso_z(value: str) -> str:
    """ISO8601文字列（Z/+00:00 等）を UTC の Z 形式に正規化して返す。

    Args:
        value: ISO 8601 の日時文字列（timezone付き）。"Z" も許容する。

    Returns:
        str: UTC/Z 形式（例: "2025-01-01T00:00:00Z"）に正規化された日時文字列。

    Raises:
        ValueError: ISO8601として解釈できない、または timezone が欠落している場合。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("datetime must be a non-empty ISO8601 string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO8601 datetime") from exc
    if dt.tzinfo is None:
        raise ValueError("timezone information is required")
    # Not the ms form: summary ids are derived from this exact period string.
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def compute_activity_summary_id(
    *, user_id: str, summary_type: str, period_start: str
) -> str:
    """(user_id, summary_type, period_start) から決定的な summary_id（UUIDv5）を生成する。

    決定式:
        summary_id = uuidv5(
            NAMESPACE_ACTIVITY_SUMMARY,
            "activity_summary:{user_id}:{summary_type}:{period_start_utc_iso_z}",
        )

    Args:
        user_id: 対象ユーザーID（UUIDv4文字列を想定）。
        summary_type: サマリ種別（"1h" / "24h" / "1w" / "1m" / "3m"）。
        period_start: 対象期間の開始時刻（ISO 8601、timezone付き）。

    Returns:
        str: UUID文字列。

    Raises:
        ValueError: 入力が不正な場合。
    """
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("user_id is required")
    if not isinstance(summary_type, str) or not summary_type:
        raise ValueError("summary_type is required")
    period_start_z = normalize_iso_to_iso_z(period_start)
    name = f"activity_summary:{user_id}:{summary_type}:{period_start_z}"
    return str(uuid.uuid5(NAMESPACE_ACTIVITY_SUMMARY, name))

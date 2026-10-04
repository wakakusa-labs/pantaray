"""Memory source coverage と時間窓ポリシーを提供する。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Literal, TypedDict

MemorySourceId = Literal[
    "long_term_insight",
    "short_term_insight",
    "facts",
    "suggestions",
    "actions",
    "activity_summary",
    "activity_description",
]
MemorySourceStatus = Literal["present", "missing", "unknown"]


class MemorySourceCoverageSlot(TypedDict):
    """memory source ごとの存在統計スロット。"""

    source: MemorySourceId
    status: MemorySourceStatus
    latest_at: str | None


class MemorySourceCoverageSnapshot(TypedDict):
    """memory source の存在統計スナップショット。"""

    evaluated_at: str
    slots: list[MemorySourceCoverageSlot]


MEMORY_SOURCE_ORDER: tuple[MemorySourceId, ...] = (
    "long_term_insight",
    "short_term_insight",
    "facts",
    "suggestions",
    "actions",
    "activity_summary",
    "activity_description",
)
TOTAL_MEMORY_SOURCES = len(MEMORY_SOURCE_ORDER)
STOCK_MEMORY_SOURCES: tuple[MemorySourceId, ...] = (
    "long_term_insight",
    "facts",
)
FLOW_MEMORY_SOURCES: tuple[MemorySourceId, ...] = (
    "short_term_insight",
    "activity_summary",
    "activity_description",
)
WORK_RECORD_MEMORY_SOURCES: tuple[MemorySourceId, ...] = (
    "suggestions",
    "actions",
)
MEMORY_SOURCE_GROUPS: tuple[tuple[str, tuple[MemorySourceId, ...]], ...] = (
    ("Stock knowledge", STOCK_MEMORY_SOURCES),
    ("Flow knowledge", FLOW_MEMORY_SOURCES),
    ("Agent work records", WORK_RECORD_MEMORY_SOURCES),
)

SHORT_LOOKBACK_DAYS = 7
ACTIVITY_DESCRIPTION_LOOKBACK_MINUTES = 30
ACTIVITY_DESCRIPTION_MAX_WINDOW_SECONDS = ACTIVITY_DESCRIPTION_LOOKBACK_MINUTES * 60
# SHA-256(empty string): storage-backed source の「実質空本文」を判定するための共通指紋。
EMPTY_TEXT_SHA256_HEX = hashlib.sha256(b"").hexdigest()


def parse_utc_datetime(value: str) -> datetime:
    """ISO8601（Z許容）文字列を UTC datetime へ変換する。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def format_utc_datetime(value: datetime) -> str:
    """UTC datetime を ISO8601（Z）文字列へ変換する。"""

    # Whole seconds: bounds are string-compared with second-form activity windows.
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def build_short_lookback_range(now_utc: datetime) -> tuple[str, str]:
    """短期 source（7日窓）の既定期間を返す。"""

    start = now_utc - timedelta(days=SHORT_LOOKBACK_DAYS)
    return format_utc_datetime(start), format_utc_datetime(now_utc)


def build_activity_description_window(anchor_iso: str) -> tuple[str, str]:
    """activity_description の既定30分窓を返す。"""

    anchor_dt = parse_utc_datetime(anchor_iso)
    start_dt = anchor_dt - timedelta(minutes=ACTIVITY_DESCRIPTION_LOOKBACK_MINUTES)
    return format_utc_datetime(start_dt), format_utc_datetime(anchor_dt)


def build_unknown_memory_source_coverage_snapshot(
    *,
    evaluated_at: str,
) -> MemorySourceCoverageSnapshot:
    """全 source を unknown として初期化した coverage snapshot を返す。"""

    return {
        "evaluated_at": evaluated_at,
        "slots": [
            {"source": source, "status": "unknown", "latest_at": None}
            for source in MEMORY_SOURCE_ORDER
        ],
    }

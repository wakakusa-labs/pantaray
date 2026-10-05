"""Memory source の時間窓ポリシーを提供する。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

SHORT_LOOKBACK_DAYS = 7
# SHA-256(empty string): storage-backed source の「実質空本文」を判定するための共通指紋。
EMPTY_TEXT_SHA256_HEX = hashlib.sha256(b"").hexdigest()


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

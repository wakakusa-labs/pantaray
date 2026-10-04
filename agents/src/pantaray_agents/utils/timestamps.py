"""UTC timestamp formatting and parsing.

The canonical form for timestamps the local runtime stores or compares is UTC
milliseconds with a ``Z`` suffix (``YYYY-MM-DDTHH:MM:SS.sssZ``); conversation
history rejects anything else. Produce the current time with
``local_runtime.runtime.utc_timestamps.now_utc_iso``.

The microsecond form exists only for Action terminal payloads and run times,
whose already-stored values use it.
"""

from __future__ import annotations

from datetime import UTC, datetime

_MICROSECOND_UTC_TIMESTAMP_SUFFIX_FORMAT = "-%m-%dT%H:%M:%S.%fZ"


def format_iso8601_utc_z_milliseconds(value: datetime) -> str:
    """datetime を `YYYY-MM-DDTHH:MM:SS.sssZ` へ正規化する。"""

    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def normalize_iso8601_utc_z_milliseconds(value: str) -> str:
    """ISO8601 datetime を UTC/Z millisecond 文字列へ正規化する。"""

    return format_iso8601_utc_z_milliseconds(parse_iso8601_utc(value))


def normalize_iso8601_utc_z_microseconds(value: str) -> str:
    """ISO8601 datetime を `YYYY-MM-DDTHH:MM:SS.ffffffZ` へ正規化する。"""

    parsed = parse_iso8601_utc(value)
    return (
        f"{parsed.year:04d}{parsed.strftime(_MICROSECOND_UTC_TIMESTAMP_SUFFIX_FORMAT)}"
    )


def parse_iso8601_utc(value: str) -> datetime:
    """ISO8601 datetime を timezone-aware UTC datetime として parse する。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("datetime must be a non-empty ISO8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("timezone information is required")
    try:
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("datetime exceeds supported UTC range") from exc

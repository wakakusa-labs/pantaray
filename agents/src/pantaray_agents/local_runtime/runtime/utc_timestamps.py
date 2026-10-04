from __future__ import annotations

from datetime import UTC, datetime

from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.utils.timestamps import (
    format_iso8601_utc_z_milliseconds,
    parse_iso8601_utc,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def format_utc_iso(value: datetime) -> str:
    return format_iso8601_utc_z_milliseconds(value)


def now_utc_iso() -> str:
    return format_utc_iso(utc_now())


def parse_utc_iso(value: str) -> datetime:
    try:
        return parse_iso8601_utc(value)
    except ValueError as exc:
        raise MigrationError(f"{exc}: {value}") from exc


__all__ = [
    "format_utc_iso",
    "now_utc_iso",
    "parse_utc_iso",
    "utc_now",
]

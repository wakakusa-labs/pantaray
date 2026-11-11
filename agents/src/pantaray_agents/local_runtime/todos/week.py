"""Dates on the user's local calendar."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.todos.model import LocalDate
from pantaray_agents.utils.timestamps import parse_iso8601_utc


def local_date_at(at: str, zone: ZoneInfo) -> LocalDate:
    """The calendar date a UTC timestamp falls on in `zone`."""

    return parse_iso8601_utc(at).astimezone(zone).date().isoformat()


def shift_days(day: LocalDate, days: int) -> LocalDate:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()

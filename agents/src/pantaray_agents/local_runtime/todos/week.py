"""Weeks run Monday to Sunday on the user's local calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.todos.model import LocalDate, Todo
from pantaray_agents.utils.timestamps import parse_iso8601_utc


def local_date_at(at: str, zone: ZoneInfo) -> LocalDate:
    """The calendar date a UTC timestamp falls on in `zone`."""

    return parse_iso8601_utc(at).astimezone(zone).date().isoformat()


def week_start_of(day: LocalDate) -> LocalDate:
    parsed = date.fromisoformat(day)
    return (parsed - timedelta(days=parsed.weekday())).isoformat()


def shift_days(day: LocalDate, days: int) -> LocalDate:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


@dataclass(frozen=True, slots=True)
class WeekPlacement:
    week_start: LocalDate
    carried: bool


def effective_week(
    todo: Todo, current_week_start: LocalDate, zone: ZoneInfo
) -> WeekPlacement | None:
    """The week whose list shows `todo`; None once it is deleted.

    A TODO's own week holds its start date. An open TODO from a past week is
    carried into the current week. A checked TODO stays in the week it was
    checked, so a past week lists what started in it and finished there plus
    what was carried into it and finished there. Checking early never moves a
    TODO before its own week.
    """

    if todo.deletion is not None:
        return None
    own_week = week_start_of(todo.starts_on)
    if todo.check is None:
        shown = max(own_week, current_week_start)
    else:
        checked_week = week_start_of(local_date_at(todo.check.at, zone))
        shown = max(own_week, checked_week)
    return WeekPlacement(week_start=shown, carried=shown != own_week)

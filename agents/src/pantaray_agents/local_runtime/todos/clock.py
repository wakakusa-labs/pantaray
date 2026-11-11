"""Read the clock and the user's zone once, by `read_todo_clock`, for the core."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.todos.model import LocalDate
from pantaray_agents.local_runtime.todos.week import local_date_at
from pantaray_agents.utils.local_time import local_zone_name


class UnknownTimeZoneError(RuntimeError):
    """The OS zone has no IANA name, so local dates cannot be computed."""


@dataclass(frozen=True, slots=True)
class TodoClock:
    now: str
    zone: ZoneInfo

    @property
    def today(self) -> LocalDate:
        return local_date_at(self.now, self.zone)


def read_todo_clock() -> TodoClock:
    """The only place the TODO list reads the clock and the user's zone."""

    zone_name = local_zone_name()
    if zone_name is None:
        raise UnknownTimeZoneError("the OS time zone has no IANA name")
    return TodoClock(now=now_utc_iso(), zone=ZoneInfo(zone_name))

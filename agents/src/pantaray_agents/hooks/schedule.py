"""When a trigger fires: pure functions of the trigger, an instant and a zone.

Grid arithmetic runs on the wall clock of the trigger's zone, so a daily hook
in a local zone stays at its local time across daylight-saving changes. A wall
time occurs at the first instant the zone's clock reads it or later: an
ambiguous one (clocks set back) at its first pass, a missing one (clocks set
forward) at the jump. Occurrences are therefore non-decreasing in UTC, and
missing wall times that share a jump collapse into one occurrence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

from .definitions import At, CalendarUnit, Every


def latest_occurrence(trigger: At | Every, now: datetime, zone: tzinfo) -> datetime:
    """The latest occurrence at or before ``now``; for an unaligned trigger, now.

    After missed occurrences (sleep, restart, a closed gate) only this latest
    one fires, once.
    """
    # Instants are compared and added to in UTC: wall-clock arithmetic on a local
    # `now` would step backwards across a clock set back.
    now = now.astimezone(UTC)
    if isinstance(trigger, At):
        return trigger.at.astimezone(UTC)
    if not trigger.align:
        return now
    return _instant(_latest_boundary(trigger, now, zone) + trigger.offset, zone)


def next_occurrence_after(
    trigger: At | Every, moment: datetime, zone: tzinfo
) -> datetime | None:
    """The first occurrence after ``moment``, or None when the trigger is spent."""
    if isinstance(trigger, At):
        return None
    moment = moment.astimezone(UTC)
    if not trigger.align and isinstance(trigger.every, timedelta):
        return moment + trigger.every
    boundary = _step(_latest_boundary(trigger, moment, zone), trigger.every)
    return _instant(boundary + trigger.offset, zone)


def _latest_boundary(trigger: Every, now: datetime, zone: tzinfo) -> datetime:
    """The wall-clock boundary of the latest occurrence at or before ``now``.

    Flooring the wall clock can land behind the latest occurrence after the
    clocks were set back, so step forward while the next one is still due.
    """
    boundary = _floor(_wall(now, zone) - trigger.offset, trigger.every)
    while _instant(_step(boundary, trigger.every) + trigger.offset, zone) <= now:
        boundary = _step(boundary, trigger.every)
    return boundary


def _wall(moment: datetime, zone: tzinfo) -> datetime:
    return moment.astimezone(zone).replace(tzinfo=None)


def _instant(wall: datetime, zone: tzinfo) -> datetime:
    """The first instant the clock of ``zone`` reads ``wall`` or later."""
    instant = wall.replace(tzinfo=zone).astimezone(UTC)
    shown = _wall(instant, zone)
    if shown == wall:
        return instant
    # Missing: `instant` is past the jump by as much as its clock is past
    # `wall`, so the jump lies within that span. Transitions fall on whole
    # seconds; search for the first second whose clock reads `wall` or later.
    low, high = instant - (shown - wall), instant
    while high - low > timedelta(seconds=1):
        middle = low + timedelta(seconds=(high - low).total_seconds() // 2)
        if _wall(middle, zone) >= wall:
            high = middle
        else:
            low = middle
    return high


def _floor(wall: datetime, every: timedelta | CalendarUnit) -> datetime:
    day = wall.replace(hour=0, minute=0, second=0, microsecond=0)
    if isinstance(every, timedelta):
        return day + (wall - day) // every * every
    if every == "hour":
        return wall.replace(minute=0, second=0, microsecond=0)
    if every == "day":
        return day
    if every == "week":
        return day - timedelta(days=day.weekday())
    if every == "month":
        return day.replace(day=1)
    return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)


def _step(slot: datetime, every: timedelta | CalendarUnit) -> datetime:
    if isinstance(every, timedelta):
        return slot + every
    if every == "hour":
        return slot + timedelta(hours=1)
    if every == "day":
        return slot + timedelta(days=1)
    if every == "week":
        return slot + timedelta(weeks=1)
    months = slot.month - 1 + (1 if every == "month" else 3)
    return slot.replace(year=slot.year + months // 12, month=months % 12 + 1)

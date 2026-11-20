"""When a trigger fires: pure functions of the trigger, an instant and a zone.

Grid arithmetic runs on the wall clock of the trigger's zone, so a daily hook
in a local zone stays at its local time across daylight-saving changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

from .definitions import At, CalendarUnit, Trigger


def latest_occurrence(trigger: Trigger, now: datetime, zone: tzinfo) -> datetime:
    """The occurrence a fire at ``now`` stands for.

    After missed occurrences (sleep, restart, a closed gate) only this latest
    one fires, once.
    """
    if isinstance(trigger, At):
        return trigger.at.astimezone(UTC)
    if not trigger.align:
        return now
    slot = _floor(_wall(now, zone) - trigger.offset, trigger.every)
    return _instant(slot + trigger.offset, zone)


def next_occurrence_after(
    trigger: Trigger, moment: datetime, zone: tzinfo
) -> datetime | None:
    """The first occurrence after ``moment``, or None when the trigger is spent."""
    if isinstance(trigger, At):
        return None
    if not trigger.align and isinstance(trigger.every, timedelta):
        return moment + trigger.every
    slot = _floor(_wall(moment, zone) - trigger.offset, trigger.every)
    return _instant(_step(slot, trigger.every) + trigger.offset, zone)


def _wall(moment: datetime, zone: tzinfo) -> datetime:
    return moment.astimezone(zone).replace(tzinfo=None)


def _instant(wall: datetime, zone: tzinfo) -> datetime:
    return wall.replace(tzinfo=zone).astimezone(UTC)


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

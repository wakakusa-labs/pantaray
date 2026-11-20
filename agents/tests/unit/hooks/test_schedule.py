from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pantaray_agents.hooks.definitions import At, CalendarUnit, Every
from pantaray_agents.hooks.schedule import latest_occurrence, next_occurrence_after


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _calendar(unit: CalendarUnit, minutes: int) -> Every:
    return Every(every=unit, align=True, offset=timedelta(minutes=minutes), tz="utc")


@pytest.mark.parametrize(
    ("trigger", "now", "latest", "following"),
    [
        # Inside the offset the previous boundary's occurrence is still latest.
        (
            _calendar("hour", 5),
            _utc(2026, 4, 15, 10, 3),
            _utc(2026, 4, 15, 9, 5),
            _utc(2026, 4, 15, 10, 5),
        ),
        (
            _calendar("day", 10),
            _utc(2026, 12, 31, 23, 0),
            _utc(2026, 12, 31, 0, 10),
            _utc(2027, 1, 1, 0, 10),
        ),
        # A week starts on Monday: Sunday belongs to the week begun six days ago.
        (
            _calendar("week", 15),
            _utc(2026, 11, 22, 12, 0),
            _utc(2026, 11, 16, 0, 15),
            _utc(2026, 11, 23, 0, 15),
        ),
        (
            _calendar("month", 20),
            _utc(2026, 12, 15, 0, 0),
            _utc(2026, 12, 1, 0, 20),
            _utc(2027, 1, 1, 0, 20),
        ),
        (
            _calendar("month", 20),
            _utc(2027, 3, 1, 0, 19),
            _utc(2027, 2, 1, 0, 20),
            _utc(2027, 3, 1, 0, 20),
        ),
        (
            _calendar("quarter", 30),
            _utc(2026, 11, 21, 0, 0),
            _utc(2026, 10, 1, 0, 30),
            _utc(2027, 1, 1, 0, 30),
        ),
        (
            _calendar("quarter", 30),
            _utc(2027, 4, 1, 0, 10),
            _utc(2027, 1, 1, 0, 30),
            _utc(2027, 4, 1, 0, 30),
        ),
        (
            Every(
                every=timedelta(minutes=15), align=True, offset=timedelta(0), tz="utc"
            ),
            _utc(2026, 9, 7, 23, 59, 59),
            _utc(2026, 9, 7, 23, 45),
            _utc(2026, 9, 8, 0, 0),
        ),
    ],
)
def test_grid_occurrences(
    trigger: Every, now: datetime, latest: datetime, following: datetime
) -> None:
    # Catches month/quarter rollover at year end, a week starting on the wrong
    # day, and an offset applied after flooring (which fires an hour early).
    assert latest_occurrence(trigger, now, UTC) == latest
    assert next_occurrence_after(trigger, now, UTC) == following
    assert next_occurrence_after(trigger, following, UTC) != following


def test_a_local_daily_hook_keeps_its_wall_time_across_a_dst_change() -> None:
    # Catches grid arithmetic done in UTC, which drifts an hour off local 07:00.
    zone = ZoneInfo("Europe/Berlin")
    trigger = Every(every="day", align=True, offset=timedelta(hours=7), tz="local")
    before = datetime(2026, 3, 28, 8, 0, tzinfo=zone)

    following = next_occurrence_after(trigger, before, zone)

    assert following is not None
    assert following.astimezone(zone) == datetime(2026, 3, 29, 7, 0, tzinfo=zone)
    assert following.utcoffset() == timedelta(0)
    assert following == _utc(2026, 3, 29, 5, 0)


def test_an_unaligned_interval_counts_from_the_fire() -> None:
    trigger = Every(
        every=timedelta(hours=2), align=False, offset=timedelta(0), tz="utc"
    )
    fired = _utc(2026, 9, 7, 10, 7, 31)

    assert latest_occurrence(trigger, fired, UTC) == fired
    assert next_occurrence_after(trigger, fired, UTC) == fired + timedelta(hours=2)


def test_a_one_shot_trigger_is_spent_after_it_fires() -> None:
    at = datetime(2026, 9, 7, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    assert latest_occurrence(At(at=at), _utc(2026, 9, 8), UTC) == _utc(2026, 9, 7, 0)
    assert next_occurrence_after(At(at=at), _utc(2026, 9, 8), UTC) is None


_BERLIN = ZoneInfo("Europe/Berlin")
# Berlin sets clocks forward at 01:00Z on 2026-03-29 (02:00 -> 03:00 local) and
# back at 01:00Z on 2026-10-25 (03:00 -> 02:00 local).
_TRANSITIONS = (_utc(2026, 3, 29, 1), _utc(2026, 10, 25, 1))
_LOCAL_TRIGGERS = (
    Every(every=timedelta(minutes=15), align=True, offset=timedelta(0), tz="local"),
    Every(every="hour", align=True, offset=timedelta(minutes=30), tz="local"),
    Every(every="day", align=True, offset=timedelta(hours=2, minutes=30), tz="local"),
)


@pytest.mark.parametrize("trigger", _LOCAL_TRIGGERS)
@pytest.mark.parametrize("transition", _TRANSITIONS)
def test_occurrences_bracket_every_instant_around_a_dst_change(
    trigger: Every, transition: datetime
) -> None:
    # Catches a next occurrence in the past after clocks are set back (a hook
    # would refire every tick) and a latest one in the future after they are
    # set forward (it would fire early, then again at its time).
    instant = transition - timedelta(hours=3)
    while instant < transition + timedelta(hours=3):
        latest = latest_occurrence(trigger, instant, _BERLIN)
        following = next_occurrence_after(trigger, instant, _BERLIN)
        assert following is not None
        assert latest <= instant < following, instant
        assert next_occurrence_after(trigger, latest, _BERLIN) == following, instant
        instant += timedelta(seconds=150)


def test_ambiguous_and_missing_wall_times_occur_at_the_first_reading() -> None:
    # Pins the rule: a repeated wall time fires on its first pass, a skipped
    # one when the clocks jump past it.
    half_past = Every(
        every="hour", align=True, offset=timedelta(minutes=30), tz="local"
    )

    assert next_occurrence_after(half_past, _utc(2026, 3, 29, 0, 45), _BERLIN) == (
        _utc(2026, 3, 29, 1, 0)
    )
    assert next_occurrence_after(half_past, _utc(2026, 3, 29, 1, 0), _BERLIN) == (
        _utc(2026, 3, 29, 1, 30)
    )
    assert next_occurrence_after(half_past, _utc(2026, 10, 25, 0, 0), _BERLIN) == (
        _utc(2026, 10, 25, 0, 30)
    )
    assert next_occurrence_after(half_past, _utc(2026, 10, 25, 0, 30), _BERLIN) == (
        _utc(2026, 10, 25, 2, 30)
    )

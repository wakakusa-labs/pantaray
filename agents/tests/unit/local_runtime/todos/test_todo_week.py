from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from pantaray_agents.local_runtime.todos.model import TodoCheck, TodoDeletion
from pantaray_agents.local_runtime.todos.week import (
    WeekPlacement,
    effective_week,
    local_date_at,
    week_start_of,
)

from .support import NEXT_WEEK, PREVIOUS_WEEK, WEEK, make_todo

TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("zone", "at", "expected_week"),
    [
        # Sunday 23:59 / Monday 00:00 JST; the UTC date is still Sunday at 00:00.
        (TOKYO, "2026-11-08T14:59:00.000Z", "2026-11-02"),
        (TOKYO, "2026-11-08T15:00:00.000Z", "2026-11-09"),
        # The Sunday DST ends (EDT -> EST); a fixed -04:00 lands on Monday.
        (NEW_YORK, "2026-11-02T04:59:00.000Z", "2026-10-26"),
        (NEW_YORK, "2026-11-02T05:00:00.000Z", "2026-11-02"),
        # The week after DST starts (EST -> EDT); a fixed -05:00 lands on Sunday.
        (NEW_YORK, "2026-03-09T03:59:00.000Z", "2026-03-02"),
        (NEW_YORK, "2026-03-09T04:00:00.000Z", "2026-03-09"),
    ],
)
def test_week_turns_at_local_monday_midnight(
    zone: ZoneInfo, at: str, expected_week: str
) -> None:
    assert week_start_of(local_date_at(at, zone)) == expected_week


@pytest.mark.parametrize(
    ("week_start", "checked_at", "expected"),
    [
        (PREVIOUS_WEEK, None, (WEEK, True)),
        (NEXT_WEEK, None, (NEXT_WEEK, False)),
        ("2026-10-19", "2026-10-28T03:00:00.000Z", (PREVIOUS_WEEK, True)),
        (PREVIOUS_WEEK, "2026-10-30T03:00:00.000Z", (PREVIOUS_WEEK, False)),
        (NEXT_WEEK, "2026-11-03T03:00:00.000Z", (NEXT_WEEK, False)),
    ],
    ids=[
        "open-past-is-carried",
        "open-future-stays",
        "checked-later-stays-in-check-week",
        "checked-in-own-past-week-stays",
        "checked-early-stays-in-own-week",
    ],
)
def test_effective_week(
    week_start: str, checked_at: str | None, expected: tuple[str, bool]
) -> None:
    check = None if checked_at is None else TodoCheck(by="user", at=checked_at)
    todo = make_todo(week_start=week_start, check=check)

    assert effective_week(todo, WEEK, TOKYO) == WeekPlacement(*expected)


def test_check_week_is_read_in_the_users_zone() -> None:
    # Monday 00:30 in Tokyo is still Sunday in New York.
    todo = make_todo(check=TodoCheck(by="user", at="2026-11-08T15:30:00.000Z"))

    assert effective_week(todo, NEXT_WEEK, TOKYO) == WeekPlacement(NEXT_WEEK, True)
    assert effective_week(todo, NEXT_WEEK, NEW_YORK) == WeekPlacement(WEEK, False)


def test_deleted_todo_is_in_no_week() -> None:
    deletion = TodoDeletion(by="user", at="2026-11-03T00:00:00.000Z")
    todo = make_todo(week_start=PREVIOUS_WEEK, deletion=deletion)

    assert effective_week(todo, WEEK, TOKYO) is None

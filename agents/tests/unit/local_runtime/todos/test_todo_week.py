from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from pantaray_agents.local_runtime.todos.week import local_date_at

TOKYO = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("zone", "at", "expected"),
    [
        # 23:59 / 00:00 JST; the UTC date is still the day before at 00:00.
        (TOKYO, "2026-11-08T14:59:00.000Z", "2026-11-08"),
        (TOKYO, "2026-11-08T15:00:00.000Z", "2026-11-09"),
        # The day after DST ends (EDT -> EST); a fixed -04:00 lands a day late.
        (NEW_YORK, "2026-11-02T04:59:00.000Z", "2026-11-01"),
        (NEW_YORK, "2026-11-02T05:00:00.000Z", "2026-11-02"),
        # The day after DST starts (EST -> EDT); a fixed -05:00 lands a day early.
        (NEW_YORK, "2026-03-09T03:59:00.000Z", "2026-03-08"),
        (NEW_YORK, "2026-03-09T04:00:00.000Z", "2026-03-09"),
    ],
)
def test_the_local_date_turns_at_local_midnight(
    zone: ZoneInfo, at: str, expected: str
) -> None:
    assert local_date_at(at, zone) == expected

"""Where the user's time went, summed from the short Insights' estimates."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Final

from pantaray_agents.local_runtime.activity_summary_schedule import iso_z

# The windows a Memory run sees: this week against the month around it.
TIME_WINDOWS: Final[tuple[tuple[str, timedelta], ...]] = (
    ("Last 7 days", timedelta(days=7)),
    ("Last 28 days", timedelta(days=28)),
)
# The long tail of one-off endeavors adds noise, not a picture of the split.
MAX_ENDEAVORS_PER_WINDOW: Final = 12


def render_time_by_endeavor(
    connection: sqlite3.Connection, *, user_id: str, now: datetime
) -> str:
    """Render each window's minutes per endeavor, largest first, or nothing."""
    sections: list[str] = []
    for label, length in TIME_WINDOWS:
        since = iso_z(now - length)
        rows = connection.execute(
            """SELECT endeavor, organization_name, project_name, SUM(minutes)
               FROM activity_time_allocations
               WHERE user_id = ? AND observed_start >= ?
               GROUP BY endeavor, organization_name, project_name
               ORDER BY SUM(minutes) DESC, endeavor""",
            (user_id, since),
        ).fetchall()
        total = sum(int(row[3]) for row in rows)
        if not total:
            continue
        lines = [f"{label} (since {since}, {total / 60:.1f}h observed):"]
        for endeavor, organization, project, minutes in rows[:MAX_ENDEAVORS_PER_WINDOW]:
            where = " / ".join(name for name in (organization, project) if name)
            name = f"{endeavor} ({where})" if where else str(endeavor)
            lines.append(
                f"- {name}: {int(minutes) / 60:.1f}h ({round(100 * int(minutes) / total)}%)"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)

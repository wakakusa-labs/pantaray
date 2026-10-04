from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

SummaryType = Literal["1h", "24h", "1w", "1m", "3m"]


@dataclass(frozen=True, slots=True)
class ActivitySummaryScheduleSpec:
    job_name: str
    summary_type: SummaryType
    grace_seconds: int


@dataclass(frozen=True, slots=True)
class ActivitySummaryWindow:
    period_start: datetime
    period_end: datetime


# Ordered shortest period first: every type but the first is aggregated from the
# summaries of the type before it, so the order is also the aggregation chain.
ACTIVITY_SUMMARY_SCHEDULE_SPECS: tuple[ActivitySummaryScheduleSpec, ...] = (
    ActivitySummaryScheduleSpec("activity_summary_1h", "1h", 5 * 60),
    ActivitySummaryScheduleSpec("activity_summary_24h", "24h", 10 * 60),
    ActivitySummaryScheduleSpec("activity_summary_1w", "1w", 15 * 60),
    ActivitySummaryScheduleSpec("activity_summary_1m", "1m", 20 * 60),
    ActivitySummaryScheduleSpec("activity_summary_3m", "3m", 30 * 60),
)


def shorter_summary_types(summary_type: SummaryType) -> tuple[SummaryType, ...]:
    """The types whose summaries this one aggregates, directly or through the chain."""
    ordered = tuple(spec.summary_type for spec in ACTIVITY_SUMMARY_SCHEDULE_SPECS)
    return ordered[: ordered.index(summary_type)]


def iso_z(value: datetime) -> str:
    # Not the ms form: period bounds and scheduler cursors match stored strings.
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def period_end_floor(moment: datetime, summary_type: SummaryType) -> datetime:
    if summary_type == "1h":
        return moment.replace(minute=0, second=0, microsecond=0)
    if summary_type == "24h":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if summary_type == "1w":
        return (moment - timedelta(days=moment.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    if summary_type == "1m":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if summary_type == "3m":
        quarter_start_month = ((moment.month - 1) // 3) * 3 + 1
        return moment.replace(
            month=quarter_start_month,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    raise ValueError(f"Unknown summary_type: {summary_type}")


def previous_period_start(period_end: datetime, summary_type: SummaryType) -> datetime:
    if summary_type == "1h":
        return period_end - timedelta(hours=1)
    if summary_type == "24h":
        return period_end - timedelta(days=1)
    if summary_type == "1w":
        return period_end - timedelta(weeks=1)
    if summary_type == "1m":
        if period_end.month == 1:
            return period_end.replace(year=period_end.year - 1, month=12)
        return period_end.replace(month=period_end.month - 1)
    if summary_type == "3m":
        if period_end.month <= 3:
            return period_end.replace(
                year=period_end.year - 1,
                month=period_end.month + 9,
            )
        return period_end.replace(month=period_end.month - 3)
    raise ValueError(f"Unknown summary_type: {summary_type}")


def next_period_end(period_end: datetime, summary_type: SummaryType) -> datetime:
    if summary_type == "1h":
        return period_end + timedelta(hours=1)
    if summary_type == "24h":
        return period_end + timedelta(days=1)
    if summary_type == "1w":
        return period_end + timedelta(weeks=1)
    if summary_type == "1m":
        if period_end.month == 12:
            return period_end.replace(year=period_end.year + 1, month=1)
        return period_end.replace(month=period_end.month + 1)
    if summary_type == "3m":
        month = period_end.month + 3
        year = period_end.year
        if month > 12:
            month -= 12
            year += 1
        return period_end.replace(year=year, month=month)
    raise ValueError(f"Unknown summary_type: {summary_type}")


def latest_eligible_window(
    *, spec: ActivitySummaryScheduleSpec, now: datetime
) -> ActivitySummaryWindow:
    eligible_reference = now.astimezone(UTC) - timedelta(seconds=spec.grace_seconds)
    period_end = period_end_floor(eligible_reference, spec.summary_type)
    return ActivitySummaryWindow(
        period_start=previous_period_start(period_end, spec.summary_type),
        period_end=period_end,
    )


def window_from_start(
    *, period_start: datetime, summary_type: SummaryType
) -> ActivitySummaryWindow:
    return ActivitySummaryWindow(
        period_start=period_start,
        period_end=next_period_end(period_start, summary_type),
    )

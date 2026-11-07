"""Progress toward goals, counted from progress records and checked TODOs.

No number here comes from a model: each is a count, sum, ratio or date of the
records and TODOs whose ids travel with it. Where there is nothing to count,
the view says so instead of showing a number.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.todos.model import LocalDate, TodoId
from pantaray_agents.local_runtime.todos.week import (
    local_date_at,
    shift_days,
    week_start_of,
)
from pantaray_agents.utils.timestamps import parse_iso8601_utc

type PeriodKind = Literal["day", "week", "month"]

# Rows the view lists, newest first: a week of days, about a month of weeks,
# half a year of months.
PERIOD_ROWS: Final[Mapping[PeriodKind, int]] = {"day": 7, "week": 4, "month": 6}
# The pace comes from the last four weeks, so a weekly rhythm evens out and an
# old burst no longer drives the projection.
PACE_WINDOW_DAYS: Final = 28
# Fewer records than this in the window is one or two events, not a pace.
PACE_MIN_RECORDS: Final = 3


@dataclass(frozen=True, slots=True)
class Goal:
    """The user's goal for a project; the AI never writes one."""

    project_id: str
    text: str
    target: float
    unit: str
    deadline: LocalDate
    set_at: str  # when it was first set; records count from here


@dataclass(frozen=True, slots=True)
class UserEvidence:
    """The user wrote the record; they are its evidence."""

    kind: Literal["user"] = "user"


@dataclass(frozen=True, slots=True)
class ActionStepsEvidence:
    """Successful tool steps of the Action run that wrote the record."""

    action_id: str
    step_ids: tuple[str, ...]
    kind: Literal["action_steps"] = "action_steps"


type RecordEvidence = UserEvidence | ActionStepsEvidence


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    record_id: str
    todo_id: TodoId
    project_id: str | None  # the TODO's project now
    quantity: float
    unit: str
    note: str
    evidence: RecordEvidence
    recorded_at: str


@dataclass(frozen=True, slots=True)
class CheckedTodo:
    todo_id: TodoId
    project_id: str | None
    text: str
    checked_at: str


@dataclass(frozen=True, slots=True)
class PeriodRow:
    """One day, week or month: what was recorded and checked in it."""

    start: LocalDate
    end: LocalDate
    record_ids: tuple[str, ...]
    checked: tuple[CheckedTodo, ...]  # always shown, with or without a goal


@dataclass(frozen=True, slots=True)
class TooFewRecords:
    window_start: LocalDate
    record_ids: tuple[str, ...]  # fewer than PACE_MIN_RECORDS
    kind: Literal["too_few_records"] = "too_few_records"


@dataclass(frozen=True, slots=True)
class Reached:
    kind: Literal["reached"] = "reached"


@dataclass(frozen=True, slots=True)
class Projected:
    """The window's sum per day, carried forward linearly to the target."""

    window_start: LocalDate  # through today
    record_ids: tuple[str, ...]
    per_day: float
    projected_on: LocalDate
    kind: Literal["projected"] = "projected"


type Pace = TooFewRecords | Reached | Projected


@dataclass(frozen=True, slots=True)
class NoGoal:
    project_id: str
    kind: Literal["no_goal"] = "no_goal"


@dataclass(frozen=True, slots=True)
class NoRecords:
    goal: Goal
    # Records since the goal was set whose unit is not the goal's.
    other_unit_record_ids: tuple[str, ...]
    kind: Literal["no_records"] = "no_records"


@dataclass(frozen=True, slots=True)
class Measured:
    goal: Goal
    total: float  # the sum of `record_ids`' quantities
    share: float  # total / target; above 1 once exceeded
    record_ids: tuple[str, ...]
    other_unit_record_ids: tuple[str, ...]  # left out of the sum
    # Where a straight line from the set date to the deadline is today.
    expected_share: float
    deadline_passed: bool
    pace: Pace
    kind: Literal["measured"] = "measured"


type GoalProgress = NoGoal | NoRecords | Measured


@dataclass(frozen=True, slots=True)
class ProgressView:
    today: LocalDate
    period: PeriodKind
    rows: tuple[PeriodRow, ...]  # newest first
    goals: tuple[GoalProgress, ...]
    # Every record an id above names, newest first.
    records: tuple[ProgressRecord, ...]


def progress_view(
    *,
    period: PeriodKind,
    today: LocalDate,
    zone: ZoneInfo,
    goals: Mapping[str, Goal | None],
    records: Sequence[ProgressRecord],
    checked: Sequence[CheckedTodo],
) -> ProgressView:
    """`goals` holds, by project id, each project whose goal the view reports.

    `records` and `checked` are those of the TODOs the view covers; ones
    outside the rows or before a goal was set are left out here.
    """

    rows = tuple(
        _row(start, end, records=records, checked=checked, zone=zone)
        for start, end in period_bounds(period, today)
    )
    progress = tuple(
        NoGoal(project_id=project_id)
        if goal is None
        else _goal_progress(goal, records=records, today=today, zone=zone)
        for project_id, goal in goals.items()
    )
    named = {record_id for row in rows for record_id in row.record_ids}
    for item in progress:
        if not isinstance(item, NoGoal):
            named.update(item.other_unit_record_ids)
        if isinstance(item, Measured):
            named.update(item.record_ids)
    return ProgressView(
        today=today,
        period=period,
        rows=rows,
        goals=progress,
        records=tuple(
            sorted(
                (record for record in records if record.record_id in named),
                key=lambda record: (record.recorded_at, record.record_id),
                reverse=True,
            )
        ),
    )


def period_bounds(
    period: PeriodKind, today: LocalDate
) -> tuple[tuple[LocalDate, LocalDate], ...]:
    """The first and last day of each row, the period containing today first."""

    bounds: list[tuple[LocalDate, LocalDate]] = []
    start = _period_start(period, today)
    for _ in range(PERIOD_ROWS[period]):
        end = _next_period_start(period, start)
        bounds.append((start, shift_days(end, -1)))
        start = _period_start(period, shift_days(start, -1))
    return tuple(bounds)


def _period_start(period: PeriodKind, day: LocalDate) -> LocalDate:
    if period == "day":
        return day
    if period == "week":
        return week_start_of(day)
    return date.fromisoformat(day).replace(day=1).isoformat()


def _next_period_start(period: PeriodKind, start: LocalDate) -> LocalDate:
    if period == "day":
        return shift_days(start, 1)
    if period == "week":
        return shift_days(start, 7)
    return _period_start("month", shift_days(start, 31))


def _row(
    start: LocalDate,
    end: LocalDate,
    *,
    records: Sequence[ProgressRecord],
    checked: Sequence[CheckedTodo],
    zone: ZoneInfo,
) -> PeriodRow:
    return PeriodRow(
        start=start,
        end=end,
        record_ids=tuple(
            record.record_id
            for record in _newest_first(records)
            if start <= local_date_at(record.recorded_at, zone) <= end
        ),
        checked=tuple(
            todo
            for todo in sorted(
                checked, key=lambda todo: (todo.checked_at, todo.todo_id)
            )
            if start <= local_date_at(todo.checked_at, zone) <= end
        ),
    )


def _goal_progress(
    goal: Goal,
    *,
    records: Sequence[ProgressRecord],
    today: LocalDate,
    zone: ZoneInfo,
) -> NoRecords | Measured:
    set_at = parse_iso8601_utc(goal.set_at)
    since_set = [
        record
        for record in _newest_first(records)
        if record.project_id == goal.project_id
        and parse_iso8601_utc(record.recorded_at) >= set_at
    ]
    counted = [record for record in since_set if record.unit == goal.unit]
    other_unit = tuple(
        record.record_id for record in since_set if record.unit != goal.unit
    )
    if not counted:
        return NoRecords(goal=goal, other_unit_record_ids=other_unit)
    total = math.fsum(record.quantity for record in counted)
    set_on = local_date_at(goal.set_at, zone)
    return Measured(
        goal=goal,
        total=total,
        share=total / goal.target,
        record_ids=tuple(record.record_id for record in counted),
        other_unit_record_ids=other_unit,
        expected_share=_elapsed_share(set_on, goal.deadline, today),
        deadline_passed=today > goal.deadline,
        pace=_pace(goal, counted, total=total, set_on=set_on, today=today, zone=zone),
    )


def _elapsed_share(set_on: LocalDate, deadline: LocalDate, today: LocalDate) -> float:
    total_days = (date.fromisoformat(deadline) - date.fromisoformat(set_on)).days
    if total_days <= 0:
        return 1.0
    elapsed = (date.fromisoformat(today) - date.fromisoformat(set_on)).days
    return min(1.0, max(0.0, elapsed / total_days))


def _pace(
    goal: Goal,
    counted: Sequence[ProgressRecord],
    *,
    total: float,
    set_on: LocalDate,
    today: LocalDate,
    zone: ZoneInfo,
) -> Pace:
    if total >= goal.target:
        return Reached()
    window_start = max(set_on, shift_days(today, -(PACE_WINDOW_DAYS - 1)))
    in_window = [
        record
        for record in counted
        if local_date_at(record.recorded_at, zone) >= window_start
    ]
    record_ids = tuple(record.record_id for record in in_window)
    if len(in_window) < PACE_MIN_RECORDS:
        return TooFewRecords(window_start=window_start, record_ids=record_ids)
    window_days = (date.fromisoformat(today) - date.fromisoformat(window_start)).days
    per_day = math.fsum(record.quantity for record in in_window) / (window_days + 1)
    days_left = math.ceil((goal.target - total) / per_day)
    return Projected(
        window_start=window_start,
        record_ids=record_ids,
        per_day=per_day,
        projected_on=(
            date.fromisoformat(today) + timedelta(days=days_left)
        ).isoformat(),
    )


def _newest_first(records: Sequence[ProgressRecord]) -> list[ProgressRecord]:
    return sorted(
        records, key=lambda record: (record.recorded_at, record.record_id), reverse=True
    )

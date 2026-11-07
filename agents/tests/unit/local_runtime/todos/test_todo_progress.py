"""The progress view's numbers, each checked against the ids it names (A14)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace

from pantaray_agents.local_runtime.todos.progress import (
    PACE_MIN_RECORDS,
    CheckedTodo,
    Goal,
    Measured,
    NoGoal,
    NoRecords,
    PeriodKind,
    ProgressRecord,
    ProgressView,
    Projected,
    Reached,
    TooFewRecords,
    UserEvidence,
    period_bounds,
    progress_view,
)

from .store_support import TOKYO

# Set at 09:00 on Sunday 2026-11-01 in Tokyo; 60 days to the deadline.
GOAL = Goal(
    project_id="project-1",
    text="Close 300M yen in deals",
    target=300_000_000.0,
    unit="円",
    deadline="2026-12-31",
    set_at="2026-11-01T00:00:00.000Z",
)
TODAY = "2026-11-04"


def _record(
    record_id: str,
    recorded_at: str,
    quantity: float = 10_000_000.0,
    *,
    unit: str = "円",
    project_id: str | None = "project-1",
) -> ProgressRecord:
    return ProgressRecord(
        record_id=record_id,
        todo_id="todo-1",
        project_id=project_id,
        quantity=quantity,
        unit=unit,
        note="Signed",
        evidence=UserEvidence(),
        recorded_at=recorded_at,
    )


def _view(
    records: Sequence[ProgressRecord],
    *,
    goals: Mapping[str, Goal | None] | None = None,
    checked: Sequence[CheckedTodo] = (),
    period: PeriodKind = "week",
    today: str = TODAY,
) -> ProgressView:
    return progress_view(
        period=period,
        today=today,
        zone=TOKYO,
        goals={"project-1": GOAL} if goals is None else goals,
        records=records,
        checked=checked,
    )


def _measured(view: ProgressView) -> Measured:
    (goal,) = view.goals
    assert isinstance(goal, Measured)
    return goal


def _quantities(view: ProgressView, record_ids: Sequence[str]) -> float:
    by_id = {record.record_id: record for record in view.records}
    return math.fsum(by_id[record_id].quantity for record_id in record_ids)


def test_the_total_is_the_sum_of_the_named_records_in_the_goals_unit() -> None:
    view = _view(
        [
            _record("r-1", "2026-11-02T01:00:00.000Z"),
            _record("r-2", "2026-11-03T01:00:00.000Z", 20_000_000.0),
            _record("r-other", "2026-11-03T02:00:00.000Z", 3_000.0, unit="万円"),
            # Recorded before the goal was set, and in another project.
            _record("r-before", "2026-10-31T23:59:59.000Z"),
            _record("r-elsewhere", "2026-11-03T01:00:00.000Z", project_id="p-2"),
        ]
    )

    measured = _measured(view)
    assert measured.record_ids == ("r-2", "r-1")
    assert measured.other_unit_record_ids == ("r-other",)
    assert measured.total == _quantities(view, measured.record_ids) == 30_000_000.0
    assert measured.share == 0.1
    assert {record.record_id for record in view.records} >= {"r-1", "r-2", "r-other"}


def test_no_goal_and_no_records_carry_no_numbers() -> None:
    view = _view(
        [_record("r-other", "2026-11-03T01:00:00.000Z", 3.0, unit="件")],
        goals={"project-1": GOAL, "project-2": None},
    )

    assert view.goals == (
        NoRecords(goal=GOAL, other_unit_record_ids=("r-other",)),
        NoGoal(project_id="project-2"),
    )


def test_too_few_records_in_the_window_give_no_pace() -> None:
    records = [
        _record("r-1", "2026-11-02T01:00:00.000Z"),
        _record("r-2", "2026-11-03T01:00:00.000Z"),
    ]
    assert len(records) < PACE_MIN_RECORDS

    assert _measured(_view(records)).pace == TooFewRecords(
        window_start="2026-11-01", record_ids=("r-2", "r-1")
    )


def test_the_projection_carries_the_window_pace_forward() -> None:
    # 30M over the four days since the goal was set: 7.5M a day, 270M to go.
    view = _view(
        [
            _record("r-1", "2026-11-01T01:00:00.000Z"),
            _record("r-2", "2026-11-02T01:00:00.000Z"),
            _record("r-3", "2026-11-04T01:00:00.000Z"),
        ]
    )

    pace = _measured(view).pace
    assert isinstance(pace, Projected)
    assert pace.window_start == "2026-11-01"
    assert pace.per_day == _quantities(view, pace.record_ids) / 4 == 7_500_000.0
    assert pace.projected_on == "2026-12-10"  # 36 days after today


def test_the_pace_window_drops_records_older_than_four_weeks() -> None:
    goal = replace(GOAL, set_at="2026-09-01T00:00:00.000Z")
    view = _view(
        [
            _record("old-1", "2026-09-02T01:00:00.000Z"),
            _record("old-2", "2026-09-03T01:00:00.000Z"),
            _record("old-3", "2026-09-04T01:00:00.000Z"),
            _record("new", "2026-11-03T01:00:00.000Z"),
        ],
        goals={"project-1": goal},
    )

    assert _measured(view).pace == TooFewRecords(
        window_start="2026-10-08", record_ids=("new",)
    )


def test_a_passed_deadline_expects_the_whole_target() -> None:
    records = [_record("r-1", "2026-11-02T01:00:00.000Z")]

    on_time = _measured(_view(records))
    late = _measured(_view(records, today="2027-01-05"))

    assert (on_time.expected_share, on_time.deadline_passed) == (0.05, False)
    assert (late.expected_share, late.deadline_passed) == (1.0, True)


def test_a_reached_target_has_no_projection() -> None:
    view = _view([_record("r-1", "2026-11-02T01:00:00.000Z", 300_000_000.0)])

    assert _measured(view).pace == Reached()


def test_rows_follow_the_local_calendar_and_list_checked_todos() -> None:
    # 2026-11-01T16:00Z is Monday 01:00 in Tokyo: this week, not the last.
    monday_early = _record("r-mon", "2026-11-01T16:00:00.000Z")
    sunday_late = _record("r-sun", "2026-11-01T14:00:00.000Z")
    checked = [
        CheckedTodo(
            "todo-a", "project-1", "Send the quote", "2026-11-03T01:00:00.000Z"
        ),
        CheckedTodo("todo-b", None, "Book the room", "2026-11-03T02:00:00.000Z"),
    ]

    week = _view([monday_early, sunday_late], goals={}, checked=checked)
    day = _view([monday_early, sunday_late], goals={}, checked=checked, period="day")

    assert [(row.start, row.end, row.record_ids) for row in week.rows[:2]] == [
        ("2026-11-02", "2026-11-08", ("r-mon",)),
        ("2026-10-26", "2026-11-01", ("r-sun",)),
    ]
    assert [todo.text for todo in day.rows[1].checked] == [
        "Send the quote",
        "Book the room",
    ]
    assert day.rows[1].start == day.rows[1].end == "2026-11-03"


def test_month_rows_cover_whole_calendar_months() -> None:
    assert period_bounds("month", "2026-03-31")[:3] == (
        ("2026-03-01", "2026-03-31"),
        ("2026-02-01", "2026-02-28"),
        ("2026-01-01", "2026-01-31"),
    )

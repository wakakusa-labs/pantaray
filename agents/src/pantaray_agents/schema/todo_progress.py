"""The progress view's HTTP shapes; Electron's contracts mirror them.

Requests are validated here once. The view is the core's `ProgressView` read
attribute by attribute, so each number arrives with the ids it was counted from.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pantaray_agents.local_runtime.todos.model import LocalDate
from pantaray_agents.local_runtime.todos.progress import PeriodKind
from pantaray_agents.local_runtime.todos.store import (
    AnyProject,
    NoProject,
    OneProject,
    TodoProjectFilter,
)
from pantaray_agents.schema.todos import TodoScopeQuery

GOAL_UNIT_MAX_LENGTH = 20
GOAL_TEXT_MAX_LENGTH = 200
RECORD_NOTE_MAX_LENGTH = 200

type _Id = Annotated[str, Field(min_length=1, max_length=128)]
type _Quantity = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type _Unit = Annotated[str, Field(min_length=1, max_length=GOAL_UNIT_MAX_LENGTH)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class TodoGoalRequest(_Request):
    """`deadline` may not precede the local date the goal was first set."""

    text: Annotated[str, Field(min_length=1, max_length=GOAL_TEXT_MAX_LENGTH)]
    target: _Quantity
    unit: _Unit
    deadline: dt.date


class ProgressRecordCreateRequest(_Request):
    """`client_record_id` is the idempotency key."""

    client_record_id: _Id
    quantity: _Quantity
    unit: _Unit
    note: Annotated[str, Field(min_length=1, max_length=RECORD_NOTE_MAX_LENGTH)]


class TodoProgressQuery(TodoScopeQuery):
    """`project_id` is given exactly when `project` is ``project``."""

    project: Literal["any", "project", "no_project"]
    project_id: _Id | None = None
    period: PeriodKind

    @model_validator(mode="after")
    def _project_id_matches_filter(self) -> Self:
        if (self.project == "project") != (self.project_id is not None):
            raise ValueError("project_id is required exactly for project=project")
        return self

    def project_filter(self) -> TodoProjectFilter:
        if self.project_id is not None:
            return OneProject(project_id=self.project_id)
        return NoProject() if self.project == "no_project" else AnyProject()


class GoalView(_View):
    project_id: str
    text: str
    target: float
    unit: str
    deadline: LocalDate
    set_at: str  # records count from here


class UserEvidenceView(_View):
    kind: Literal["user"]


class ActionStepsEvidenceView(_View):
    action_id: str
    step_ids: tuple[str, ...]
    kind: Literal["action_steps"]


class ProgressRecordView(_View):
    record_id: str
    todo_id: str
    project_id: str | None
    quantity: float
    unit: str
    note: str
    # `user` is written by the user; `action_steps` by the AI.
    evidence: Annotated[
        UserEvidenceView | ActionStepsEvidenceView, Field(discriminator="kind")
    ]
    recorded_at: str


class CheckedTodoView(_View):
    todo_id: str
    project_id: str | None
    text: str
    checked_at: str


class ProgressPeriodView(_View):
    start: LocalDate
    end: LocalDate
    record_ids: tuple[str, ...]  # "N 件進みました" is their count
    checked: tuple[CheckedTodoView, ...]


class TooFewRecordsView(_View):
    window_start: LocalDate
    record_ids: tuple[str, ...]
    kind: Literal["too_few_records"]


class ReachedView(_View):
    kind: Literal["reached"]


class ProjectedView(_View):
    window_start: LocalDate
    record_ids: tuple[str, ...]
    per_day: float
    projected_on: LocalDate
    kind: Literal["projected"]


class BeyondHorizonView(_View):
    window_start: LocalDate
    record_ids: tuple[str, ...]
    per_day: float
    kind: Literal["beyond_horizon"]


class NoGoalView(_View):
    project_id: str
    kind: Literal["no_goal"]


class NoRecordsView(_View):
    goal: GoalView
    other_unit_record_ids: tuple[str, ...]
    kind: Literal["no_records"]


class MeasuredView(_View):
    goal: GoalView
    total: float
    share: float
    record_ids: tuple[str, ...]
    other_unit_record_ids: tuple[str, ...]
    expected_share: float
    deadline_passed: bool
    pace: Annotated[
        TooFewRecordsView | ReachedView | ProjectedView | BeyondHorizonView,
        Field(discriminator="kind"),
    ]
    kind: Literal["measured"]


class TodoProgressPage(_View):
    today: LocalDate  # the backend owns the clock
    period: PeriodKind
    rows: tuple[ProgressPeriodView, ...]  # newest first
    goals: tuple[
        Annotated[
            NoGoalView | NoRecordsView | MeasuredView, Field(discriminator="kind")
        ],
        ...,
    ]
    records: tuple[ProgressRecordView, ...]  # every record an id names

"""Store goals and progress records, and read what the progress view counts.

Each write is one transaction; one that changes nothing writes nothing, so a
replayed request leaves the revision the live session follows as it was.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Final, Literal

from pantaray_agents.local_runtime.runtime.identity import verify_current_owner
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.local_runtime.todos.clock import (
    TodoClock,
)
from pantaray_agents.local_runtime.todos.core import Rejected
from pantaray_agents.local_runtime.todos.db import (
    NoProject,
    OneProject,
    TodoProjectFilter,
    TodoScope,
    owns_project,
    todo_db_connection,
    todo_project_sql,
    todo_scope_sql,
)
from pantaray_agents.local_runtime.todos.model import LocalDate, TodoId
from pantaray_agents.local_runtime.todos.progress import (
    ActionStepsEvidence,
    CheckedTodo,
    Goal,
    PeriodKind,
    ProgressRecord,
    ProgressView,
    RecordEvidence,
    UserEvidence,
    period_bounds,
    progress_view,
)
from pantaray_agents.local_runtime.todos.week import local_date_at, shift_days

_NAMESPACE_PROGRESS_RECORD: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:todo_progress_record:v1"
)
_GOAL_COLUMNS: Final = "project_id, text, target_quantity, unit, deadline, set_at"
_RECORD_SELECT: Final = (
    "record.record_id, record.todo_id, todo.project_id, record.quantity, "
    "record.unit, record.note, record.evidence_action_id, record.evidence_step_ids, "
    "record.recorded_at"
)

type GoalRejection = Literal["project_not_found", "invalid_text", "deadline_before_set"]
type RecordRejection = Literal[
    "invalid_text", "deleted", "record_conflict", "evidence_not_in_run"
]


@dataclass(frozen=True, slots=True)
class AlreadyRecorded:
    """Another call recorded the same figure from the same steps; nothing added."""

    record: ProgressRecord


# The run's successful tool steps by the History Ref the model was shown; a
# retried step answers with its latest successful attempt. The run starts at
# the first user message it adopted, and no later run exists while it runs.
_RUN_TOOL_STEPS_SQL: Final = """
SELECT step.short_step_id, step.step_id FROM agent_action_steps AS step
WHERE step.user_id = :user_id AND step.action_id = :action_id
  AND step.short_step_id IN (SELECT value FROM json_each(:refs))
  AND step.step_type = 'tool_execution' AND step.status = 'success'
  AND step.step_number >= (
    SELECT MIN(opening.step_number) FROM agent_action_steps AS opening
    WHERE opening.user_id = :user_id AND opening.action_id = :action_id
      AND opening.step_type = 'user_request'
      AND opening.adopted_process_id = :process_id)
ORDER BY step.completed_at, step.step_id
"""


def set_project_goal(
    *,
    user_id: str,
    project_id: str,
    text: str,
    target: float,
    unit: str,
    deadline: LocalDate,
    clock: TodoClock,
) -> Goal | Rejected[GoalRejection]:
    """Set or edit the project's goal; an edit keeps when it was first set."""

    goal_text, goal_unit = _line(text), _line(unit)
    if not goal_text or not goal_unit:
        return Rejected("invalid_text")
    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        if not owns_project(connection, user_id=user_id, project_id=project_id):
            return Rejected("project_not_found")
        current = _read_goal(connection, user_id=user_id, project_id=project_id)
        goal = Goal(
            project_id=project_id,
            text=goal_text,
            target=target,
            unit=goal_unit,
            deadline=deadline,
            set_at=clock.now if current is None else current.set_at,
        )
        if deadline < local_date_at(goal.set_at, clock.zone):
            return Rejected("deadline_before_set")
        if goal != current:
            connection.execute(
                f"INSERT INTO todo_goals(user_id, {_GOAL_COLUMNS}) VALUES "
                "(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET "
                "text = excluded.text, target_quantity = excluded.target_quantity, "
                "unit = excluded.unit, deadline = excluded.deadline",
                (
                    user_id,
                    project_id,
                    goal.text,
                    target,
                    goal.unit,
                    deadline,
                    goal.set_at,
                ),
            )
    return goal


def clear_project_goal(
    *, user_id: str, project_id: str
) -> None | Rejected[Literal["project_not_found"]]:
    """Remove the project's goal; clearing it again changes nothing."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        if not owns_project(connection, user_id=user_id, project_id=project_id):
            return Rejected("project_not_found")
        connection.execute(
            "DELETE FROM todo_goals WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
    return None


def add_user_progress_record(
    *,
    user_id: str,
    todo_id: TodoId,
    client_record_id: str,
    quantity: float,
    unit: str,
    note: str,
    clock: TodoClock,
) -> ProgressRecord | Rejected[RecordRejection] | None:
    """The user's record; the same `client_record_id` again returns it."""

    record_id = _record_id(f"user:{user_id}:{client_record_id}")
    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        return _add_record(
            connection,
            user_id=user_id,
            todo_id=todo_id,
            record_id=record_id,
            quantity=quantity,
            unit=unit,
            note=note,
            evidence=UserEvidence(),
            clock=clock,
        )


def add_ai_progress_record(
    *,
    user_id: str,
    todo_id: TodoId,
    tool_step_id: str,
    action_id: str,
    process_id: str,
    evidence_refs: tuple[str, ...],
    quantity: float,
    unit: str,
    note: str,
    clock: TodoClock,
) -> ProgressRecord | AlreadyRecorded | Rejected[RecordRejection] | None:
    """An Action run's record, kept only when every ref is one of its tool steps.

    The record's id derives from the calling tool step, so a replayed call
    stores nothing new. A record of the same TODO, quantity and unit from the
    same steps is the same outcome told twice, and would double the total.
    """

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        rows = connection.execute(
            _RUN_TOOL_STEPS_SQL,
            {
                "user_id": user_id,
                "action_id": action_id,
                "process_id": process_id,
                "refs": json.dumps(list(evidence_refs)),
            },
        ).fetchall()
        step_by_ref = {str(row[0]): str(row[1]) for row in rows}
        if set(step_by_ref) != set(evidence_refs):
            return Rejected("evidence_not_in_run")
        record_id = _record_id(f"step:{tool_step_id}")
        same_figure = connection.execute(
            f"SELECT {_RECORD_SELECT} FROM todo_progress_records AS record "
            "JOIN todo_items AS todo ON todo.user_id = record.user_id "
            "AND todo.todo_id = record.todo_id "
            "WHERE record.user_id = ? AND record.todo_id = ? AND record.author = 'ai' "
            "AND record.quantity = ? AND record.unit = ? AND record.record_id <> ? "
            "ORDER BY record.recorded_at, record.record_id",
            (user_id, todo_id, quantity, _line(unit), record_id),
        ).fetchall()
        for row in same_figure:
            stored = _record(row)
            assert isinstance(stored.evidence, ActionStepsEvidence)
            if set(stored.evidence.step_ids) == set(step_by_ref.values()):
                return AlreadyRecorded(record=stored)
        return _add_record(
            connection,
            user_id=user_id,
            todo_id=todo_id,
            record_id=record_id,
            quantity=quantity,
            unit=unit,
            note=note,
            evidence=ActionStepsEvidence(
                action_id=action_id,
                step_ids=tuple(step_by_ref[ref] for ref in evidence_refs),
            ),
            clock=clock,
        )


def delete_progress_record(*, user_id: str, record_id: str) -> None:
    """Remove the record; one already gone changes nothing."""

    verify_current_owner(user_id)
    with todo_db_connection() as connection, immediate_transaction(connection):
        connection.execute(
            "DELETE FROM todo_progress_records WHERE user_id = ? AND record_id = ?",
            (user_id, record_id),
        )


def read_progress(
    *,
    user_id: str,
    scope: TodoScope,
    project: TodoProjectFilter,
    period: PeriodKind,
    clock: TodoClock,
) -> ProgressView:
    """The view of the TODOs `scope` and `project` cover, as of the clock.

    A goal is reported for the one project `project` names, with or without
    one, or else for each project in scope that has one.
    """

    today = clock.today
    rows_start = period_bounds(period, today)[-1][0]
    scope_sql, scope_params = todo_scope_sql(scope)
    project_sql, project_params = todo_project_sql(project)
    params: dict[str, str] = {"user_id": user_id, **scope_params, **project_params}
    with todo_db_connection() as connection:
        goals = _read_goals(
            connection, project=project, scope_sql=scope_sql, params=params
        )
        # A local date is no earlier in UTC than the day before it.
        records_from = min(
            [shift_days(rows_start, -1), *(g.set_at for g in goals.values() if g)]
        )
        record_rows = connection.execute(
            f"SELECT {_RECORD_SELECT} FROM todo_progress_records AS record "
            "JOIN todo_items AS todo ON todo.user_id = record.user_id "
            "AND todo.todo_id = record.todo_id "
            "WHERE record.user_id = :user_id AND todo.deleted_at IS NULL "
            f"AND record.recorded_at >= :records_from AND {scope_sql} AND {project_sql}",
            {**params, "records_from": records_from},
        ).fetchall()
        checked_rows = connection.execute(
            "SELECT todo.todo_id, todo.project_id, todo.text, todo.checked_at "
            "FROM todo_items AS todo WHERE todo.user_id = :user_id "
            "AND todo.deleted_at IS NULL AND todo.checked_at >= :checked_from "
            f"AND {scope_sql} AND {project_sql}",
            {**params, "checked_from": shift_days(rows_start, -1)},
        ).fetchall()
    return progress_view(
        period=period,
        today=today,
        zone=clock.zone,
        goals=goals,
        records=[_record(row) for row in record_rows],
        checked=[
            CheckedTodo(
                todo_id=row[0], project_id=row[1], text=row[2], checked_at=row[3]
            )
            for row in checked_rows
        ],
    )


def _read_goals(
    connection: sqlite3.Connection,
    *,
    project: TodoProjectFilter,
    scope_sql: str,
    params: dict[str, str],
) -> dict[str, Goal | None]:
    if isinstance(project, NoProject):
        return {}
    if isinstance(project, OneProject):
        goal = _read_goal(
            connection, user_id=params["user_id"], project_id=project.project_id
        )
        return {project.project_id: goal}
    # Aliased as `todo` so the shared scope condition reads its project_id.
    rows = connection.execute(
        f"SELECT {_GOAL_COLUMNS} FROM todo_goals AS todo "
        f"WHERE todo.user_id = :user_id AND {scope_sql} ORDER BY todo.set_at",
        params,
    ).fetchall()
    return {row[0]: _goal(row) for row in rows}


def _read_goal(
    connection: sqlite3.Connection, *, user_id: str, project_id: str
) -> Goal | None:
    row = connection.execute(
        f"SELECT {_GOAL_COLUMNS} FROM todo_goals WHERE user_id = ? AND project_id = ?",
        (user_id, project_id),
    ).fetchone()
    return None if row is None else _goal(row)


def _add_record(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    todo_id: TodoId,
    record_id: str,
    quantity: float,
    unit: str,
    note: str,
    evidence: RecordEvidence,
    clock: TodoClock,
) -> ProgressRecord | Rejected[RecordRejection] | None:
    unit, note = _line(unit), _line(note)
    if not unit or not note:
        return Rejected("invalid_text")
    todo = connection.execute(
        "SELECT project_id, deleted_at FROM todo_items WHERE user_id = ? AND todo_id = ?",
        (user_id, todo_id),
    ).fetchone()
    if todo is None:
        return None
    existing = connection.execute(
        f"SELECT {_RECORD_SELECT} FROM todo_progress_records AS record "
        "JOIN todo_items AS todo ON todo.user_id = record.user_id "
        "AND todo.todo_id = record.todo_id "
        "WHERE record.user_id = ? AND record.record_id = ?",
        (user_id, record_id),
    ).fetchone()
    if existing is not None:
        stored = _record(existing)
        if (stored.todo_id, stored.quantity, stored.unit, stored.note) != (
            todo_id,
            quantity,
            unit,
            note,
        ):
            return Rejected("record_conflict")
        return stored
    if todo[1] is not None:
        return Rejected("deleted")
    record = ProgressRecord(
        record_id=record_id,
        todo_id=todo_id,
        project_id=todo[0],
        quantity=quantity,
        unit=unit,
        note=note,
        evidence=evidence,
        recorded_at=clock.now,
    )
    run = evidence if isinstance(evidence, ActionStepsEvidence) else None
    connection.execute(
        "INSERT INTO todo_progress_records(record_id, user_id, todo_id, quantity, "
        "unit, note, author, evidence_action_id, evidence_step_ids, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record_id,
            user_id,
            todo_id,
            quantity,
            unit,
            note,
            "user" if run is None else "ai",
            None if run is None else run.action_id,
            None if run is None else json.dumps(list(run.step_ids)),
            clock.now,
        ),
    )
    return record


def _record_id(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE_PROGRESS_RECORD, key))


def _line(text: str) -> str:
    return " ".join(text.split())


def _goal(row: sqlite3.Row) -> Goal:
    return Goal(
        project_id=row[0],
        text=row[1],
        target=float(row[2]),
        unit=row[3],
        deadline=row[4],
        set_at=row[5],
    )


def _record(row: sqlite3.Row) -> ProgressRecord:
    step_ids = row["evidence_step_ids"]
    evidence: RecordEvidence = (
        UserEvidence()
        if step_ids is None
        else ActionStepsEvidence(
            action_id=row["evidence_action_id"], step_ids=tuple(json.loads(step_ids))
        )
    )
    return ProgressRecord(
        record_id=row["record_id"],
        todo_id=row["todo_id"],
        project_id=row["project_id"],
        quantity=float(row["quantity"]),
        unit=row["unit"],
        note=row["note"],
        evidence=evidence,
        recorded_at=row["recorded_at"],
    )

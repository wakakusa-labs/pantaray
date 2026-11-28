"""State transitions of one TODO: the TODO to store, or why it was refused.

Clocks and ids come from the caller; only the next recurrence's id is derived.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    ActionRun,
    AiReport,
    DeletionRequest,
    LocalDate,
    RecurrenceOrigin,
    Todo,
    TodoAuthor,
    TodoCheck,
    TodoConversation,
    TodoDeletion,
    TodoDue,
    TodoId,
    TodoOrigin,
)
from pantaray_agents.local_runtime.todos.week import local_date_at, shift_days
from pantaray_agents.utils.timestamps import (
    format_iso8601_utc_z_milliseconds,
    parse_iso8601_utc,
)

DELETED_TODO_RETENTION: Final = timedelta(days=30)
# A checked TODO stays in the list this long; the progress calendar shows the
# older ones.
CHECKED_TODO_LISTED_FOR: Final = timedelta(days=7)

_NAMESPACE_TODO_RECURRENCE: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:todo_recurrence:v1"
)


@dataclass(frozen=True, slots=True)
class Rejected[R: str]:
    reason: R


@dataclass(frozen=True, slots=True)
class CheckSet:
    todo: Todo
    # The next instance of a checked weekly TODO. Its id derives
    # from the checked TODO's id, so storing it again is a no-op.
    next_todo: Todo | None


type CreateRejection = Literal["invalid_text"]
type CheckRejection = Literal["deleted", "ai_cannot_uncheck"]
type ReportRejection = Literal["deleted"]
type DueRejection = Literal["deleted"]
type StartRejection = Literal["deleted"]
type DeletionRequestRejection = Literal["already_pending", "declined_before", "deleted"]
type DeletionAnswerRejection = Literal["stale_request"]
type UserDeletionRejection = Literal["already_deleted"]
type RestoreRejection = Literal["not_deleted", "expired"]


def create_todo(
    *,
    todo_id: TodoId,
    text: str,
    author: TodoAuthor,
    origin: TodoOrigin,
    project_id: str | None,
    organization_id: str | None,
    starts_on: LocalDate | None,
    due: TodoDue | None,
    now: str,
) -> Todo | Rejected[CreateRejection]:
    """A new open TODO the AI may pick up from `starts_on`, or not at all while
    it has no start."""

    line = text.strip()
    if line.splitlines() != [line]:
        return Rejected("invalid_text")
    return Todo(
        todo_id=todo_id,
        project_id=project_id,
        organization_id=organization_id,
        text=line,
        author=author,
        origin=origin,
        starts_on=starts_on,
        due=due,
        check=None,
        ai_report=None,
        deletion_request=None,
        deletion=None,
        created_at=now,
        deferred_run_id=None,
    )


def set_check(
    todo: Todo, *, by: TodoAuthor, checked: bool, now: str, zone: ZoneInfo
) -> CheckSet | Rejected[CheckRejection]:
    """The AI may check a TODO but never remove a check; the user may do both."""

    if todo.deletion is not None:
        return Rejected("deleted")
    if not checked:
        if by == "ai":
            return Rejected("ai_cannot_uncheck")
        return CheckSet(todo=replace(todo, check=None), next_todo=None)
    check = todo.check or TodoCheck(by=by, at=now)
    next_todo = _next_recurrence(todo, checked_at=check.at, now=now, zone=zone)
    return CheckSet(todo=replace(todo, check=check), next_todo=next_todo)


def _next_recurrence(
    todo: Todo, *, checked_at: str, now: str, zone: ZoneInfo
) -> Todo | None:
    """The first weekly occurrence due after the local date of the check; it
    starts as many days after this one's start as it is due after this one, and
    has no start when this one has none."""

    if todo.due is None or todo.due.repeat != "weekly":
        return None
    checked_on = date.fromisoformat(local_date_at(checked_at, zone))
    days_late = (checked_on - date.fromisoformat(todo.due.date)).days
    shift = 7 * max(1, days_late // 7 + 1)
    return replace(
        todo,
        todo_id=str(uuid.uuid5(_NAMESPACE_TODO_RECURRENCE, todo.todo_id)),
        origin=RecurrenceOrigin(previous_todo_id=todo.todo_id),
        starts_on=None if todo.starts_on is None else shift_days(todo.starts_on, shift),
        due=TodoDue(date=shift_days(todo.due.date, shift), repeat="weekly"),
        check=None,
        ai_report=None,
        deletion_request=None,
        deletion=None,
        created_at=now,
        deferred_run_id=None,
    )


def set_due(todo: Todo, due: TodoDue | None) -> Todo | Rejected[DueRejection]:
    if todo.deletion is not None:
        return Rejected("deleted")
    return replace(todo, due=due)


def set_start(
    todo: Todo,
    starts_on: LocalDate | None,
    *,
    conversation: TodoConversation,
    today: LocalDate,
) -> Todo | Rejected[StartRejection]:
    """The user's new start; one that puts started work back records the run
    it was put back from, and any other move only changes the date."""

    if todo.deletion is not None:
        return Rejected("deleted")
    run = deferred_run(todo, starts_on, conversation=conversation, today=today)
    if run is None:
        return replace(todo, starts_on=starts_on)
    return replace(todo, starts_on=starts_on, deferred_run_id=run.run_id)


def deferred_run(
    todo: Todo,
    starts_on: LocalDate | None,
    *,
    conversation: TodoConversation,
    today: LocalDate,
) -> ActionRun | None:
    """The run whose work this start puts back until it comes: the latest run
    of an open TODO's conversation, when the start moves past today or is
    unset. The caller stops that run if it has not ended. A start of today or
    earlier leaves the work as it is, and a done TODO's work is over."""

    if todo.check is not None or not isinstance(conversation, ActionConversation):
        return None
    if starts_on is not None and starts_on <= today:
        return None
    return conversation.latest_run


def report_progress(todo: Todo, report: AiReport) -> Todo | Rejected[ReportRejection]:
    if todo.deletion is not None:
        return Rejected("deleted")
    return replace(todo, ai_report=report)


def request_deletion(
    todo: Todo, *, request_id: str, reason: str, now: str
) -> Todo | Rejected[DeletionRequestRejection]:
    """An AI request to delete; only the user's approval deletes."""

    if todo.deletion is not None:
        return Rejected("deleted")
    if todo.deletion_request is not None:
        if todo.deletion_request.state == "pending":
            return Rejected("already_pending")
        return Rejected("declined_before")
    return replace(
        todo,
        deletion_request=DeletionRequest(
            request_id=request_id, reason=reason, requested_at=now, state="pending"
        ),
    )


def answer_deletion(
    todo: Todo, *, request_id: str, answer: Literal["approve", "keep"], now: str
) -> Todo | Rejected[DeletionAnswerRejection]:
    request = todo.deletion_request
    if (
        todo.deletion is not None
        or request is None
        or request.request_id != request_id
        or request.state != "pending"
    ):
        return Rejected("stale_request")
    if answer == "keep":
        # The declined request stays on the row so the AI cannot ask again.
        return replace(todo, deletion_request=replace(request, state="declined"))
    return replace(
        todo,
        deletion_request=None,
        deletion=TodoDeletion(by="ai_approved_by_user", at=now),
    )


def delete_by_user(todo: Todo, *, now: str) -> Todo | Rejected[UserDeletionRejection]:
    if todo.deletion is not None:
        return Rejected("already_deleted")
    request = todo.deletion_request
    # A pending request is moot once the user deleted the TODO; dropping it keeps
    # an answer to it from deleting the TODO again after a restore. A declined
    # one stays so the AI still cannot ask again.
    if request is not None and request.state == "pending":
        request = None
    return replace(
        todo, deletion=TodoDeletion(by="user", at=now), deletion_request=request
    )


def restore(todo: Todo, *, now: str) -> Todo | Rejected[RestoreRejection]:
    """Back to its own project, or organization, and start date."""

    if todo.deletion is None:
        return Rejected("not_deleted")
    if purge_due(todo, now=now):
        return Rejected("expired")
    return replace(todo, deletion=None)


def listed_checked_since(now: str) -> str:
    """The earliest check time a TODO the list shows may have."""

    return format_iso8601_utc_z_milliseconds(
        parse_iso8601_utc(now) - CHECKED_TODO_LISTED_FOR
    )


def purge_due(todo: Todo, *, now: str) -> bool:
    """Whether the retention period since deletion has fully passed."""

    if todo.deletion is None:
        return False
    deleted_at = parse_iso8601_utc(todo.deletion.at)
    return parse_iso8601_utc(now) >= deleted_at + DELETED_TODO_RETENTION

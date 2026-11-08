"""TODO list facts, and the Action facts the list only reads (`TodoConversation`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type TodoId = str
type LocalDate = str  # The user's calendar date, YYYY-MM-DD.
type TodoAuthor = Literal["user", "ai"]


@dataclass(frozen=True, slots=True)
class WrittenOrigin:
    kind: Literal["written"] = "written"


@dataclass(frozen=True, slots=True)
class SuggestionOrigin:
    suggestion_id: str
    kind: Literal["suggestion"] = "suggestion"


@dataclass(frozen=True, slots=True)
class RecurrenceOrigin:
    previous_todo_id: TodoId
    kind: Literal["recurrence"] = "recurrence"


type TodoOrigin = WrittenOrigin | SuggestionOrigin | RecurrenceOrigin


@dataclass(frozen=True, slots=True)
class TodoDue:
    # Weekly recurrence lives here so a repeating TODO without a due date
    # cannot be represented.
    date: LocalDate
    repeat: Literal["none", "weekly"]


@dataclass(frozen=True, slots=True)
class TodoCheck:
    by: TodoAuthor
    at: str


@dataclass(frozen=True, slots=True)
class AiReport:
    """What the AI last said about the work; finishing is a check, not a report."""

    kind: Literal["continue", "partial", "needs_user"]
    note: str
    # Process id of the Action run that wrote it; None when a Suggestion wrote it.
    run_id: str | None
    at: str


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    request_id: str
    reason: str
    requested_at: str
    state: Literal["pending", "declined"]


@dataclass(frozen=True, slots=True)
class TodoDeletion:
    by: Literal["user", "ai_approved_by_user"]
    at: str


@dataclass(frozen=True, slots=True)
class Todo:
    todo_id: TodoId
    project_id: str | None
    text: str
    author: TodoAuthor
    origin: TodoOrigin
    week_start: LocalDate  # Monday of the week it was written for; never rewritten.
    due: TodoDue | None
    check: TodoCheck | None
    ai_report: AiReport | None
    deletion_request: DeletionRequest | None
    deletion: TodoDeletion | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ActiveRun:
    run_id: str
    kind: Literal["running"] = "running"


@dataclass(frozen=True, slots=True)
class ApprovalPendingRun:
    run_id: str
    kind: Literal["approval_pending"] = "approval_pending"


@dataclass(frozen=True, slots=True)
class EndedRun:
    run_id: str
    outcome: Literal["success", "error", "canceled"]
    # First line of the latest answer, as the history row shows it.
    answer_line: str | None
    kind: Literal["ended"] = "ended"


type ActionRun = ActiveRun | ApprovalPendingRun | EndedRun


@dataclass(frozen=True, slots=True)
class NoConversation:
    kind: Literal["none"] = "none"


@dataclass(frozen=True, slots=True)
class SuggestionConversation:
    suggestion_id: str
    # The user dismissed it, so nothing continues its work.
    answered: bool
    kind: Literal["suggestion"] = "suggestion"


@dataclass(frozen=True, slots=True)
class ActionConversation:
    action_id: str
    latest_run: ActionRun
    kind: Literal["action"] = "action"


type TodoConversation = NoConversation | SuggestionConversation | ActionConversation

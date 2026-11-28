"""TODO list facts, and the Action facts the list only reads (`TodoConversation`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pantaray_agents.schema.agent.action_message import FileAttachmentInput
from pantaray_agents.schema.agent.image import ImageInput

type TodoId = str
type LocalDate = str  # The user's calendar date, YYYY-MM-DD.
type TodoAuthor = Literal["user", "ai"]


@dataclass(frozen=True, slots=True)
class WrittenOrigin:
    # What the user attached when writing it, held until the Action that works
    # on it takes them in its first message.
    # Only a written TODO holds any, so a weekly instance, which is a
    # `RecurrenceOrigin`, never claims files the first Action already took.
    files: tuple[FileAttachmentInput, ...] = ()
    images: tuple[ImageInput, ...] = ()
    kind: Literal["written"] = "written"


@dataclass(frozen=True, slots=True)
class SuggestionOrigin:
    suggestion_id: str
    kind: Literal["suggestion"] = "suggestion"


@dataclass(frozen=True, slots=True)
class RecurrenceOrigin:
    previous_todo_id: TodoId
    kind: Literal["recurrence"] = "recurrence"


@dataclass(frozen=True, slots=True)
class HookOrigin:
    # The conversation that asked for the hook whose fire wrote this TODO to
    # open its conversation. Provenance only: deleting this TODO leaves the
    # hook, and deleting that conversation leaves this TODO.
    origin_action_id: str
    kind: Literal["hook"] = "hook"


type TodoOrigin = WrittenOrigin | SuggestionOrigin | RecurrenceOrigin | HookOrigin


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
    # Its own organization, only while it has no project: a TODO in a project
    # is in the project's organization.
    organization_id: str | None
    text: str
    author: TodoAuthor
    origin: TodoOrigin
    # The AI picks it up from this date, or never while it is unset; the user
    # may move it.
    starts_on: LocalDate | None
    due: TodoDue | None
    check: TodoCheck | None
    ai_report: AiReport | None
    deletion_request: DeletionRequest | None
    deletion: TodoDeletion | None
    created_at: str
    # The run of its conversation the user put the work back from by moving
    # the start past today. While that run is still the latest and has ended,
    # the TODO waits for its start as one not started, and on that day the
    # pickup sends the conversation its next turn. A newer run, however it
    # came, makes it stale.
    deferred_run_id: str | None


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

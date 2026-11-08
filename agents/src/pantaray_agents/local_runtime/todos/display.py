"""What a TODO row shows and whether the pickup should start work on it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, assert_never

from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    ActiveRun,
    AiReport,
    ApprovalPendingRun,
    EndedRun,
    LocalDate,
    NoConversation,
    SuggestionConversation,
    Todo,
    TodoAuthor,
    TodoConversation,
    TodoId,
)


@dataclass(frozen=True, slots=True)
class Done:
    by: TodoAuthor
    at: str
    kind: Literal["done"] = "done"


@dataclass(frozen=True, slots=True)
class Working:
    note: str | None
    kind: Literal["working"] = "working"


@dataclass(frozen=True, slots=True)
class Waiting:
    reason: Literal["approval", "reply"]
    note: str | None
    kind: Literal["waiting"] = "waiting"


@dataclass(frozen=True, slots=True)
class Partial:
    note: str | None
    kind: Literal["partial"] = "partial"


@dataclass(frozen=True, slots=True)
class Stopped:
    outcome: Literal["failed", "canceled"]
    kind: Literal["stopped"] = "stopped"


@dataclass(frozen=True, slots=True)
class NotStarted:
    reason: Literal["week_not_started", "starting"]
    kind: Literal["not_started"] = "not_started"


type TodoDisplayState = Done | Working | Waiting | Partial | Stopped | NotStarted


@dataclass(frozen=True, slots=True)
class StartWritten:
    todo_id: TodoId
    kind: Literal["start_written"] = "start_written"


@dataclass(frozen=True, slots=True)
class ContinueAiTodo:
    todo_id: TodoId
    kind: Literal["continue_ai_todo"] = "continue_ai_todo"


type PickupTarget = StartWritten | ContinueAiTodo


def display_state(
    todo: Todo, conversation: TodoConversation, current_week_start: LocalDate
) -> TodoDisplayState:
    if todo.check is not None:
        return Done(by=todo.check.by, at=todo.check.at)
    match conversation:
        case ActionConversation():
            return _action_state(todo, conversation)
        case SuggestionConversation(answered=answered):
            return _suggestion_state(todo, answered)
        case NoConversation():
            return _not_started(todo, current_week_start)
        case _:
            assert_never(conversation)


def _action_state(todo: Todo, conversation: ActionConversation) -> TodoDisplayState:
    run = conversation.latest_run
    # Only the latest run's report counts: after the user replies, a stale
    # "needs your reply" from the run before must not outlive the next run.
    report = todo.ai_report
    if report is not None and report.run_id != run.run_id:
        report = None
    note = None if report is None else report.note
    match run:
        case ActiveRun():
            return Working(note=note)
        case ApprovalPendingRun():
            return Waiting(reason="approval", note=note)
        case EndedRun(outcome="error"):
            return Stopped(outcome="failed")
        case EndedRun(outcome="canceled"):
            return Stopped(outcome="canceled")
        case EndedRun():
            if report is None:
                return Partial(note=run.answer_line)
            return _reported_state(report)
        case _:
            assert_never(run)


def _suggestion_state(todo: Todo, answered: bool) -> TodoDisplayState:
    report = todo.ai_report
    if report is None:
        # A done Suggestion TODO the user unchecked: the Suggestion's reply
        # flow is how it continues.
        return Waiting(reason="reply", note=None)
    # Nothing starts this work but the user's answer; once they dismissed the
    # Suggestion, its work stays where it stopped.
    if report.kind == "continue" and not answered:
        return Waiting(reason="approval", note=report.note)
    return _reported_state(report)


def _reported_state(report: AiReport) -> TodoDisplayState:
    if report.kind == "needs_user":
        return Waiting(reason="reply", note=report.note)
    return Partial(note=report.note)


def _not_started(todo: Todo, current_week_start: LocalDate) -> NotStarted:
    if todo.week_start > current_week_start:
        return NotStarted(reason="week_not_started")
    return NotStarted(reason="starting")


def pickup_target(
    todo: Todo, conversation: TodoConversation, current_week_start: LocalDate
) -> PickupTarget | None:
    if (
        todo.check is not None
        or todo.deletion is not None
        or todo.week_start > current_week_start
        or conversation.kind == "action"
    ):
        return None
    # A Suggestion's TODO is worked on in the Suggestion's own conversation,
    # which only the user's approval or reply opens.
    if todo.origin.kind == "suggestion":
        return None
    if todo.author == "user":
        return StartWritten(todo_id=todo.todo_id)
    return ContinueAiTodo(todo_id=todo.todo_id)

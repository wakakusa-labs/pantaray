from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest

from pantaray_agents.local_runtime.todos.display import (
    ContinueAiTodo,
    Done,
    NotStarted,
    Partial,
    StartWritten,
    Stopped,
    Waiting,
    Working,
    display_state,
    pickup_target,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    ActiveRun,
    AiReport,
    ApprovalPendingRun,
    EndedRun,
    NoConversation,
    RecurrenceOrigin,
    SuggestionConversation,
    SuggestionOrigin,
    Todo,
    TodoCheck,
    TodoConversation,
    TodoDeletion,
)

from .support import PREVIOUS_WEEK, make_todo

type ReportKind = Literal["continue", "partial", "needs_user"]

AT = "2026-11-04T09:00:00.000Z"
TODAY = "2026-11-04"
TOMORROW = "2026-11-05"
SUGGESTION = SuggestionConversation(suggestion_id="sug-1", answered=False)
DISMISSED = SuggestionConversation(suggestion_id="sug-1", answered=True)
RUNNING = ActionConversation("action-1", ActiveRun("run-2"))
APPROVAL = ActionConversation("action-1", ApprovalPendingRun("run-2"))
SUCCEEDED = ActionConversation("action-1", EndedRun("run-2", "success", "Answer"))
FAILED = ActionConversation("action-1", EndedRun("run-2", "error", "Answer"))
CANCELED = ActionConversation("action-1", EndedRun("run-2", "canceled", "Answer"))
NONE = NoConversation()
START = StartWritten("todo-1")
CONTINUE = ContinueAiTodo("todo-1")
SUGGESTED_TODO = make_todo(author="ai", origin=SuggestionOrigin("sug-1"))


def _reported(kind: ReportKind, run_id: str | None = "run-2") -> Todo:
    report = AiReport(kind=kind, note=f"{kind} note", run_id=run_id, at=AT)
    return replace(SUGGESTED_TODO, ai_report=report)


@pytest.mark.parametrize(
    ("todo", "conversation", "expected"),
    [
        (make_todo(check=TodoCheck(by="ai", at=AT)), RUNNING, Done("ai", AT)),
        (_reported("partial"), RUNNING, Working("partial note")),
        (_reported("partial"), APPROVAL, Waiting("approval", "partial note")),
        # A note from the run before is stale once a new run starts.
        (_reported("needs_user", "run-1"), RUNNING, Working(None)),
        (_reported("needs_user"), FAILED, Stopped("failed")),
        (_reported("needs_user"), CANCELED, Stopped("canceled")),
        (_reported("needs_user"), SUCCEEDED, Waiting("reply", "needs_user note")),
        (_reported("partial"), SUCCEEDED, Partial("partial note")),
        # The user replied to run-1's question and run-2 ended without a report.
        (_reported("needs_user", "run-1"), SUCCEEDED, Partial("Answer")),
        (
            _reported("needs_user", None),
            SUGGESTION,
            Waiting("reply", "needs_user note"),
        ),
        (_reported("partial", None), SUGGESTION, Partial("partial note")),
        # Nothing but the user's answer starts it, so it is not "about to".
        (
            _reported("continue", None),
            SUGGESTION,
            Waiting("approval", "continue note"),
        ),
        # Dismissed, nothing continues it: it stays where the Suggestion left it.
        (_reported("continue", None), DISMISSED, Partial("continue note")),
        # A done Suggestion TODO the user unchecked.
        (SUGGESTED_TODO, SUGGESTION, Waiting("reply", None)),
        (make_todo(starts_on=TOMORROW), NONE, NotStarted("scheduled")),
        (make_todo(starts_on=TODAY), NONE, NotStarted("starting")),
        (make_todo(starts_on=None), NONE, NotStarted("unscheduled")),
    ],
)
def test_display_state(
    todo: Todo, conversation: TodoConversation, expected: object
) -> None:
    assert display_state(todo, conversation, TODAY) == expected


@pytest.mark.parametrize(
    ("todo", "conversation", "expected"),
    [
        (make_todo(), NONE, START),
        (make_todo(starts_on=PREVIOUS_WEEK), NONE, START),
        (make_todo(starts_on=TODAY), NONE, START),
        (make_todo(origin=RecurrenceOrigin("todo-0")), NONE, START),
        (make_todo(author="ai", origin=RecurrenceOrigin("todo-0")), NONE, CONTINUE),
        # Only the user's approval or reply opens a Suggestion's work, however
        # it left the TODO.
        (_reported("continue", None), SUGGESTION, None),
        (_reported("continue", None), DISMISSED, None),
        (_reported("continue", None), NONE, None),
        (_reported("continue", None), SUCCEEDED, None),
        (_reported("partial", None), SUGGESTION, None),
        (SUGGESTED_TODO, SUGGESTION, None),
        # Tomorrow, though in the week that holds today.
        (make_todo(starts_on=TOMORROW), NONE, None),
        (make_todo(starts_on=None), NONE, None),
        (make_todo(check=TodoCheck(by="user", at=AT)), NONE, None),
        (make_todo(deletion=TodoDeletion(by="user", at=AT)), NONE, None),
        (make_todo(), SUCCEEDED, None),
    ],
)
def test_pickup_target(
    todo: Todo, conversation: TodoConversation, expected: object
) -> None:
    assert pickup_target(todo, conversation, TODAY) == expected

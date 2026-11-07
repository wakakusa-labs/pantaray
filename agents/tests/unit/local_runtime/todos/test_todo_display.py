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

from .support import NEXT_WEEK, PREVIOUS_WEEK, WEEK, make_todo

type ReportKind = Literal["continue", "partial", "needs_user"]

AT = "2026-11-04T09:00:00.000Z"
SUGGESTION = SuggestionConversation(suggestion_id="sug-1")
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
        (_reported("continue", None), SUGGESTION, NotStarted("starting")),
        # A done Suggestion TODO the user unchecked.
        (SUGGESTED_TODO, SUGGESTION, Waiting("reply", None)),
        (
            make_todo(week_start=NEXT_WEEK),
            NONE,
            NotStarted("week_not_started"),
        ),
        (make_todo(week_start=PREVIOUS_WEEK), NONE, NotStarted("starting")),
    ],
)
def test_display_state(
    todo: Todo, conversation: TodoConversation, expected: object
) -> None:
    assert display_state(todo, conversation, WEEK) == expected


@pytest.mark.parametrize(
    ("todo", "conversation", "expected"),
    [
        (make_todo(), NONE, START),
        (make_todo(week_start=PREVIOUS_WEEK), NONE, START),
        (make_todo(origin=RecurrenceOrigin("todo-0")), NONE, START),
        (make_todo(author="ai", origin=RecurrenceOrigin("todo-0")), NONE, CONTINUE),
        (_reported("continue", None), SUGGESTION, CONTINUE),
        (_reported("partial", None), SUGGESTION, None),
        (SUGGESTED_TODO, SUGGESTION, None),
        (make_todo(week_start=NEXT_WEEK), NONE, None),
        (make_todo(check=TodoCheck(by="user", at=AT)), NONE, None),
        (make_todo(deletion=TodoDeletion(by="user", at=AT)), NONE, None),
        (make_todo(), SUCCEEDED, None),
    ],
)
def test_pickup_target(
    todo: Todo, conversation: TodoConversation, expected: object
) -> None:
    assert pickup_target(todo, conversation, WEEK) == expected

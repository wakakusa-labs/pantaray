from __future__ import annotations

from dataclasses import replace

from pantaray_agents.local_runtime.todos.model import Todo, TodoDue, WrittenOrigin

# 2026-11-02 is a Monday.
WEEK = "2026-11-02"
NEXT_WEEK = "2026-11-09"
PREVIOUS_WEEK = "2026-10-26"

_BASE = Todo(
    todo_id="todo-1",
    project_id="project-1",
    text="Send the quote",
    author="user",
    origin=WrittenOrigin(),
    starts_on=WEEK,
    due=None,
    check=None,
    ai_report=None,
    deletion_request=None,
    deletion=None,
    created_at="2026-11-02T01:00:00.000Z",
)

WEEKLY_DUE = TodoDue(date="2026-11-08", repeat="weekly")


def make_todo(**changes: object) -> Todo:
    return replace(_BASE, **changes)

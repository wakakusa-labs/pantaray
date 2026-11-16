"""The TODO a shown Suggestion became, as the Suggestion's popup names it."""

from __future__ import annotations

from pantaray_agents.local_runtime.todos.clock import read_todo_clock
from pantaray_agents.local_runtime.todos.display import (
    Done,
    Partial,
    Waiting,
    display_state,
)
from pantaray_agents.local_runtime.todos.reads import read_todo
from pantaray_agents.local_runtime.todos.writes import suggestion_todo_id
from pantaray_agents.schema.overlay_bootstrap import SuggestionTodoView


def read_suggestion_todo(
    *, user_id: str, suggestion_id: str
) -> SuggestionTodoView | None:
    """None when the Suggestion has no TODO: the list was off, or it was not shown.

    The state is the TODO list's, read now; it is left out once an Action
    carries the work, whose conversation then shows how it stands.
    """

    entry = read_todo(user_id=user_id, todo_id=suggestion_todo_id(suggestion_id))
    if entry is None:
        return None
    if entry.conversation.kind == "action":
        return SuggestionTodoView(text=entry.todo.text, state=None)
    state = display_state(entry.todo, entry.conversation, read_todo_clock().today)
    # Without an Action a Suggestion's TODO is only ever done, partial or waiting.
    if not isinstance(state, Done | Partial | Waiting):
        raise ValueError(f"A Suggestion's TODO without an Action is {state.kind}")
    return SuggestionTodoView(text=entry.todo.text, state=state.kind)

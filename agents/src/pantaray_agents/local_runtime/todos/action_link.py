"""Find the TODO an Action or a conversation serves, from the Action's own rows."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Final

from pantaray_agents.local_runtime.todos.db import todo_db_connection
from pantaray_agents.local_runtime.todos.model import TodoId

# The pickup opens a TODO's Action with a message whose id derives from the TODO
# id. SQLite cannot derive that id, but the same first message carries the
# TODO id in its hand-off, which only the pickup writes. A Suggestion's TODO is
# also served by the Action that accepted or replied to that Suggestion. Each
# branch is a lookup by key.
_HANDOFF_TODO_ID_SQL: Final = """(
  SELECT json_extract(step.user_message_json, '$.todo_handoff.todo_id')
  FROM agent_actions AS action
  JOIN agent_action_steps AS step
    ON step.user_id = action.user_id AND step.action_id = action.action_id
   AND step.user_message_id = action.initial_user_message_id
  WHERE action.user_id = :user_id AND action.action_id = :action_id)"""
_SERVED_TODO_SQL: Final = f"""
SELECT COALESCE(
  (SELECT todo_id FROM todo_items
   WHERE user_id = :user_id AND todo_id = {_HANDOFF_TODO_ID_SQL}),
  (SELECT todo.todo_id FROM agent_actions AS action
   JOIN todo_items AS todo
     ON todo.user_id = action.user_id AND todo.suggestion_id = action.suggestion_id
   WHERE action.user_id = :user_id AND action.action_id = :action_id),
  (SELECT todo.todo_id FROM agent_action_steps AS step
   JOIN todo_items AS todo
     ON todo.user_id = step.user_id AND todo.suggestion_id = step.source_suggestion_id
   WHERE step.user_id = :user_id AND step.action_id = :action_id)
)
"""
# The same links seen from a conversation being deleted, which already knows
# every Suggestion it holds.
_OPEN_TODO_OF_CONVERSATION_SQL: Final = f"""
SELECT EXISTS (SELECT 1 FROM todo_items
               WHERE user_id = :user_id AND deleted_at IS NULL
                 AND todo_id = {_HANDOFF_TODO_ID_SQL})
    OR EXISTS (SELECT 1 FROM todo_items
               WHERE user_id = :user_id AND deleted_at IS NULL
                 AND suggestion_id IN (SELECT value FROM json_each(:suggestion_ids)))
"""


def read_todo_for_action(*, user_id: str, action_id: str) -> TodoId | None:
    """The TODO whose conversation is this Action, or None for any other Action."""

    with todo_db_connection() as connection:
        row = connection.execute(
            _SERVED_TODO_SQL, {"user_id": user_id, "action_id": action_id}
        ).fetchone()
    return None if row[0] is None else str(row[0])


def conversation_has_open_todo(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str | None,
    suggestion_ids: Sequence[str],
) -> bool:
    """Whether a TODO that is not deleted has this conversation as its own.

    Read in the caller's transaction. Such a conversation goes only with its
    TODO: deleted alone, the pickup would find the TODO unstarted and run it
    again.
    """

    row = connection.execute(
        _OPEN_TODO_OF_CONVERSATION_SQL,
        {
            "user_id": user_id,
            "action_id": action_id,
            "suggestion_ids": json.dumps(list(suggestion_ids)),
        },
    ).fetchone()
    return bool(row[0])

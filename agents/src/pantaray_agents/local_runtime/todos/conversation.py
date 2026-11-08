"""Find each TODO's conversation by reading the Action and Suggestion tables.

A TODO never stores the Action it started: the Action's own unique keys tie
them, so starting it twice cannot make two. This module only reads them.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from typing import Final, Literal, cast

from pantaray_agents.local_runtime.action_conversation.errors import (
    ActionConversationIntegrityError,
)
from pantaray_agents.local_runtime.action_conversation.history_row_summary import (
    answer_status_line,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    ActionRun,
    ActiveRun,
    ApprovalPendingRun,
    EndedRun,
    NoConversation,
    SuggestionConversation,
    SuggestionOrigin,
    Todo,
    TodoConversation,
    TodoId,
)

_NAMESPACE_TODO_MESSAGE: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:todo_conversation_message:v1"
)


def todo_message_id(todo_id: TodoId) -> str:
    """The id of the first message of the Action that works on ``todo_id``.

    The pickup submits it as the message id, so
    ``UNIQUE (user_id, initial_user_message_id)`` keeps one Action per TODO.
    """

    return str(uuid.uuid5(_NAMESPACE_TODO_MESSAGE, todo_id))


# A Suggestion's conversation is the Action that accepted it or that opened
# with its message; any TODO's is also the Action opened with its own message id, which is how the
# pickup starts one. An Action's latest run is the newest adopted USER row's,
# and agent_actions keeps that run's status and answer; any paused process of
# the Action, a subagent's too, is waiting on the user's approval.
_RESOLVE_SQL = """
WITH wanted AS (
  SELECT json_extract(value, '$[0]') AS todo_id,
         json_extract(value, '$[1]') AS message_id,
         json_extract(value, '$[2]') AS suggestion_id
  FROM json_each(:wanted)
), linked AS (
  SELECT wanted.todo_id, wanted.suggestion_id,
    COALESCE(
      (SELECT action_id FROM agent_actions
       WHERE user_id = :user_id AND suggestion_id = wanted.suggestion_id),
      (SELECT action_id FROM agent_action_steps
       WHERE user_id = :user_id AND source_suggestion_id = wanted.suggestion_id),
      (SELECT action_id FROM agent_actions
       WHERE user_id = :user_id AND initial_user_message_id = wanted.message_id)
    ) AS action_id,
    EXISTS (SELECT 1 FROM agent_suggestions
            WHERE user_id = :user_id AND suggestion_id = wanted.suggestion_id)
      AS suggestion_exists,
    EXISTS (SELECT 1 FROM agent_suggestions
            WHERE user_id = :user_id AND suggestion_id = wanted.suggestion_id
              AND user_reaction IS NOT NULL) AS suggestion_answered
  FROM wanted
)
SELECT linked.todo_id, linked.suggestion_id, linked.suggestion_exists,
  linked.action_id, action.status, action.final_output,
  EXISTS (SELECT 1 FROM processes
          WHERE user_id = :user_id AND action_id = linked.action_id
            AND status = 'paused') AS paused,
  (SELECT adopted_process_id FROM agent_action_steps
   WHERE user_id = :user_id AND action_id = linked.action_id
     AND adopted_process_id IS NOT NULL
   ORDER BY accepted_sequence DESC LIMIT 1) AS latest_run_id,
  linked.suggestion_answered
FROM linked
LEFT JOIN agent_actions AS action
  ON action.user_id = :user_id AND action.action_id = linked.action_id
"""

type _ActionStatus = Literal["queued", "processing", "success", "error", "canceled"]


def resolve_todo_conversations(
    connection: sqlite3.Connection, *, user_id: str, todos: Sequence[Todo]
) -> dict[TodoId, TodoConversation]:
    """Each TODO's conversation, read in the caller's snapshot."""

    if not todos:
        return {}
    wanted = [
        [
            todo.todo_id,
            todo_message_id(todo.todo_id),
            todo.origin.suggestion_id
            if isinstance(todo.origin, SuggestionOrigin)
            else None,
        ]
        for todo in todos
    ]
    rows = connection.execute(
        _RESOLVE_SQL, {"user_id": user_id, "wanted": json.dumps(wanted)}
    ).fetchall()
    return {str(row[0]): _conversation(row) for row in rows}


def _conversation(row: sqlite3.Row) -> TodoConversation:
    _, suggestion_id, suggestion_exists, action_id, status, final_output = row[:6]
    paused, latest_run_id, answered = row[6], row[7], row[8]
    if action_id is None:
        if suggestion_id is not None and suggestion_exists:
            return SuggestionConversation(
                suggestion_id=str(suggestion_id), answered=bool(answered)
            )
        return NoConversation()
    if latest_run_id is None:
        raise ActionConversationIntegrityError("A TODO's Action has no adopted run")
    return ActionConversation(
        action_id=str(action_id),
        latest_run=_run(
            run_id=str(latest_run_id),
            status=cast("_ActionStatus", status),
            paused=bool(paused),
            final_output=str(final_output),
        ),
    )


def _run(
    *, run_id: str, status: _ActionStatus, paused: bool, final_output: str
) -> ActionRun:
    if status in ("queued", "processing"):
        return ApprovalPendingRun(run_id=run_id) if paused else ActiveRun(run_id=run_id)
    return EndedRun(
        run_id=run_id,
        outcome=cast("Literal['success', 'error', 'canceled']", status),
        answer_line=answer_status_line(final_output),
    )


__all__ = ["resolve_todo_conversations", "todo_message_id"]

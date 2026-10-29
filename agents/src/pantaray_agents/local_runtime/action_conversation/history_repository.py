"""Caller-owned snapshot composition for the public conversation history page."""

from __future__ import annotations

import sqlite3

from pantaray_agents.local_runtime.runtime.action_logical_run_authority import (
    select_action_logical_run_authorities_in_connection,
)
from pantaray_agents.schema.conversation_history import (
    ConversationHistoryListItem,
    ConversationHistoryPage,
)

from .history_candidates import (
    ConversationHistoryItemKey,
    read_conversation_history_answers_in_connection,
    read_conversation_history_candidates_in_connection,
)
from .history_projection import (
    project_conversation_history_page,
    required_history_run_selections,
)


def read_conversation_history_page_in_connection(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    search_text: str,
    cursor: str | None,
    limit: int,
    item: ConversationHistoryItemKey | None = None,
) -> ConversationHistoryPage:
    """Read one strict public page inside the caller's configured transaction."""

    normalized_search = search_text.strip().casefold()
    candidates = read_conversation_history_candidates_in_connection(
        connection=connection,
        user_id=user_id,
        search_text=normalized_search,
        cursor=cursor,
        limit=limit,
        item=item,
    )
    authorities = select_action_logical_run_authorities_in_connection(
        connection=connection,
        user_id=user_id,
        selections=required_history_run_selections(candidates.candidates),
    )
    answers = read_conversation_history_answers_in_connection(
        connection=connection, user_id=user_id, candidates=candidates.candidates
    )
    return project_conversation_history_page(
        candidate_page=candidates,
        user_id=user_id,
        normalized_search=normalized_search,
        authorities=authorities,
        answers=answers,
    )


def read_conversation_history_item_in_connection(
    *, connection: sqlite3.Connection, user_id: str, item: ConversationHistoryItemKey
) -> ConversationHistoryListItem | None:
    """The list's row for ``item``, titled as the list titles it, if the list
    holds it for ``user_id``. A suggestion that became a task -- accepted, or
    replied to -- is that task's row, as the list shows it now."""

    kind, item_id = item
    if kind == "suggestion":
        started = connection.execute(_STARTED_ACTION_SQL, (user_id, item_id)).fetchone()
        if started is not None:
            item = ("conversation", str(started[0]))
    page = read_conversation_history_page_in_connection(
        connection=connection,
        user_id=user_id,
        search_text="",
        cursor=None,
        limit=1,
        item=item,
    )
    return page.items[0] if page.items else None


# The two ways a suggestion leaves the list for a task, as the list's suggestion
# lane excludes it: an accepted offer's Action, or an Action replying to it.
_STARTED_ACTION_SQL = """
SELECT action_id FROM agent_actions WHERE user_id = ?1 AND suggestion_id = ?2
UNION ALL
SELECT action_id FROM agent_action_steps
WHERE user_id = ?1 AND source_suggestion_id = ?2
LIMIT 1
"""


__all__ = [
    "read_conversation_history_item_in_connection",
    "read_conversation_history_page_in_connection",
]

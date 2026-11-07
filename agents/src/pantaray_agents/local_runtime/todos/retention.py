"""Erase deleted TODOs once `DELETED_TODO_RETENTION` has passed, conversations too.

The conversation goes first: if the row's removal then fails, the next pass
finds the TODO with no conversation and finishes, while the reverse order could
leave a conversation nothing points at.
"""

from __future__ import annotations

import logging

from pantaray_agents.local_runtime.action_conversation.history_deletion import (
    ConversationBusyError,
    HistoryItemKind,
    delete_history_item,
)
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.todos.model import (
    ActionConversation,
    SuggestionOrigin,
)
from pantaray_agents.local_runtime.todos.store import (
    TodoEntry,
    delete_purged_todo,
    read_purge_due_todos,
)

logger = logging.getLogger(__name__)


def purge_expired_todos(*, user_id: str, now: str) -> None:
    """Erase every TODO past retention whose conversation is not running.

    A TODO whose conversation still runs or waits stays for the next pass.
    """

    for entry in read_purge_due_todos(user_id=user_id, now=now):
        try:
            for kind, item_id in _conversation_items(entry):
                _delete_conversation(user_id=user_id, kind=kind, item_id=item_id)
        except ConversationBusyError:
            logger.info(
                "Deleted TODO kept past retention: reason=conversation_busy todo_id=%s",
                entry.todo.todo_id,
            )
            continue
        delete_purged_todo(user_id=user_id, todo=entry.todo)
        logger.info("Deleted TODO purged: todo_id=%s", entry.todo.todo_id)


def _conversation_items(entry: TodoEntry) -> tuple[tuple[HistoryItemKind, str], ...]:
    items: list[tuple[HistoryItemKind, str]] = []
    if isinstance(entry.conversation, ActionConversation):
        items.append(("conversation", entry.conversation.action_id))
    # A Suggestion's TODO picked up by its own message id opens an Action that
    # is not linked to the Suggestion, so the Suggestion goes by its own id.
    origin = entry.todo.origin
    if isinstance(origin, SuggestionOrigin):
        items.append(("suggestion", origin.suggestion_id))
    return tuple(items)


def _delete_conversation(*, user_id: str, kind: HistoryItemKind, item_id: str) -> None:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    delete_history_item(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        artifact_root=read_local_runtime_artifact_root(),
        user_id=user_id,
        kind=kind,
        item_id=item_id,
    )

"""Tell the chat what happened to its work: new suggestions, and its runs' ends.

The chat reads what the Suggestion and Action sides have already stored; they
never write to the chat. A suggestion delivered since the chat began, which the
user has not reacted to, becomes a `suggestion_event`. A run the chat started
or sent a message into becomes an `action_event` when it waits for an approval
and when it ends. Each item is keyed by the event it reports, so telling it
again appends nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from pantaray_agents.local_runtime.chat.store import append_chat_item
from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.chat import (
    CHAT_ACTION_EVENT_EXCERPT_MAX_CODEPOINTS,
    ActionEventContent,
    ChatActionEventKind,
    ChatItemContent,
    SuggestionEventContent,
)

# A run's root process status once it has ended, as the chat tells it.
_RUN_ENDS: Final[dict[str, ChatActionEventKind]] = {
    "completed": "completed",
    "success": "completed",
    "failed": "failed",
    "error": "failed",
    "abandoned": "failed",
    "canceled": "canceled",
}


@dataclass(frozen=True, slots=True)
class _Event:
    at: str
    message_id: str
    content: ChatItemContent


def bridge_chat_events(*, user_id: str) -> int:
    """Append the events the chat has not been told yet; how many were new."""

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        events = _events(connection, user_id=user_id)
        if not events:
            return 0
        told = {
            str(row[0])
            for row in connection.execute(
                "SELECT message_id FROM chat_items WHERE user_id = ? AND message_id "
                f"IN ({', '.join('?' * len(events))})",
                (user_id, *(event.message_id for event in events)),
            )
        }
    new = sorted(
        (event for event in events if event.message_id not in told),
        key=lambda event: event.at,
    )
    for event in new:
        append_chat_item(
            user_id=user_id, message_id=event.message_id, content=event.content
        )
    return len(new)


def _events(connection: sqlite3.Connection, *, user_id: str) -> list[_Event]:
    began = connection.execute(
        "SELECT MIN(created_at) FROM chat_items WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if began is None:
        return []  # no chat yet: suggestions keep to the Overlay alone
    events = [
        _Event(
            at=str(created_at),
            message_id=f"suggestion:{suggestion_id}",
            content=SuggestionEventContent(
                kind="suggestion_event", suggestion_id=str(suggestion_id)
            ),
        )
        for suggestion_id, created_at in connection.execute(
            "SELECT suggestion_id, created_at FROM agent_suggestions "
            "WHERE user_id = ? AND status = 'success' AND has_suggestion = 1 "
            "AND user_reaction IS NULL AND created_at >= ?",
            (user_id, began),
        )
    ]
    runs = connection.execute(
        """
        SELECT DISTINCT steps.action_id, runs.process_id, runs.status,
               runs.updated_at, actions.final_output
        FROM agent_action_steps AS steps
        JOIN processes AS runs ON runs.process_id = steps.adopted_process_id
        JOIN agent_actions AS actions ON actions.action_id = steps.action_id
        WHERE steps.user_id = ? AND steps.user_message_id GLOB 'chat-turn/*'
        """,
        (user_id,),
    ).fetchall()
    for action_id, process_id, status, updated_at, final_output in runs:
        end = _RUN_ENDS.get(str(status))
        if end is not None:
            events.append(
                _action_event(
                    str(updated_at),
                    f"action-run:{process_id}:end",
                    str(action_id),
                    end,
                    str(final_output),
                )
            )
        events.extend(
            _action_event(
                str(created_at),
                f"action-run:{process_id}:pause:{event_id}",
                str(action_id),
                "approval_pending",
                "",
            )
            for event_id, created_at in connection.execute(
                "SELECT event_id, created_at FROM process_events "
                "WHERE process_id = ? AND event_name = 'process_paused'",
                (process_id,),
            )
        )
    return events


def _action_event(
    at: str, message_id: str, action_id: str, event: ChatActionEventKind, answer: str
) -> _Event:
    excerpt = answer.strip()[:CHAT_ACTION_EVENT_EXCERPT_MAX_CODEPOINTS] or None
    return _Event(
        at=at,
        message_id=message_id,
        content=ActionEventContent(
            kind="action_event",
            action_id=action_id,
            event=event,
            final_answer_excerpt=excerpt,
        ),
    )


__all__ = ["bridge_chat_events"]

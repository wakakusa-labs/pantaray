"""Tell the chat what happened to its own work: its runs' approval waits and ends.

The chat reads what the Action side has already stored; it never writes to the
chat. A run the chat started or sent a message into becomes an `action_event`
when it waits for an approval and when it ends. Each item is keyed by the event
it reports, so telling it again appends nothing. Work started elsewhere, a
suggestion included, stays out of the chat.
"""

from __future__ import annotations

import json
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
    ActionEventContent,
    ChatActionEventKind,
    ChatItemContent,
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
    events: list[_Event] = []
    runs = connection.execute(
        """
        SELECT DISTINCT steps.action_id, runs.process_id, runs.status,
               ends.created_at, ends.payload_json
        FROM agent_action_steps AS steps
        JOIN processes AS runs ON runs.process_id = steps.adopted_process_id
        LEFT JOIN process_events AS ends
          ON ends.process_id = runs.process_id
         AND ends.event_id = runs.terminal_event_id
        WHERE steps.user_id = ? AND steps.user_message_id GLOB 'chat-turn/*'
        """,
        (user_id,),
    ).fetchall()
    for action_id, process_id, status, ended_at, terminal_json in runs:
        end = _RUN_ENDS.get(str(status))
        if end is not None and terminal_json is not None:
            # The run's own answer, as its end recorded it: the Action's
            # current one may already belong to a later run.
            terminal = json.loads(terminal_json)
            answer = terminal.get("final_output") or terminal.get(
                "failure_message_public"
            )
            events.append(
                _action_event(
                    str(ended_at),
                    f"action-run:{process_id}:end",
                    str(action_id),
                    end,
                    answer if isinstance(answer, str) else "",
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
    excerpt = answer.strip() or None
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

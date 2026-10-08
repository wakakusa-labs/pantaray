"""The work a chat turn is shown: its own tasks and its open suggestions.

The tasks are the Actions the chat started, sent a message to or took up from
a suggestion, found by the `chat-turn/` key every such message is submitted
under. The suggestions are the action offers the user has not reacted to.
Both lists are bounded and newest first; the ids they show are what a turn
routes to, after the items that named them have left its window.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.action_conversation import ActionStatus
from pantaray_agents.tasks.action_user_message import (
    parse_action_user_message,
    render_action_user_request_text,
)

CHAT_WORK_LIST_MAX_TASKS: Final[int] = 10
CHAT_WORK_LIST_MAX_SUGGESTIONS: Final[int] = 5
# One line each: enough to tell two tasks apart, short enough for every turn.
_LINE_MAX_CHARS: Final[int] = 120


@dataclass(frozen=True, slots=True)
class ChatTask:
    action_id: str
    status: ActionStatus
    title: str
    latest: str | None  # the first line of its last answer


@dataclass(frozen=True, slots=True)
class ChatSuggestion:
    suggestion_id: str
    title: str


@dataclass(frozen=True, slots=True)
class ChatWorkList:
    tasks: tuple[ChatTask, ...]
    suggestions: tuple[ChatSuggestion, ...]


def read_chat_work_list(*, user_id: str) -> ChatWorkList:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        task_rows = connection.execute(
            """
            SELECT actions.action_id, actions.status, actions.final_output,
                   first.user_message_json
            FROM agent_actions AS actions
            JOIN agent_action_steps AS first
              ON first.user_id = actions.user_id
             AND first.user_message_id = actions.initial_user_message_id
            WHERE actions.user_id = ? AND actions.action_id IN (
                SELECT action_id FROM agent_action_steps
                WHERE user_id = ? AND user_message_id GLOB 'chat-turn/*'
            )
            ORDER BY actions.updated_at DESC, actions.action_id
            LIMIT ?
            """,
            (user_id, user_id, CHAT_WORK_LIST_MAX_TASKS),
        ).fetchall()
        suggestion_rows = connection.execute(
            """
            SELECT suggestion_id, COALESCE(suggestion_summary, answer)
            FROM agent_suggestions
            WHERE user_id = ? AND status = 'success' AND user_reaction IS NULL
              AND interaction_contract = 'action_offer'
            ORDER BY created_at DESC, suggestion_id
            LIMIT ?
            """,
            (user_id, CHAT_WORK_LIST_MAX_SUGGESTIONS),
        ).fetchall()
    return ChatWorkList(
        tasks=tuple(
            ChatTask(
                action_id=str(action_id),
                status=status,
                title=_line(
                    render_action_user_request_text(
                        parse_action_user_message(str(message_json))
                    )
                )
                or "(untitled)",
                latest=_line(str(final_output)),
            )
            for action_id, status, final_output, message_json in task_rows
        ),
        suggestions=tuple(
            ChatSuggestion(
                suggestion_id=str(suggestion_id),
                title=_line(text or "") or "(untitled)",
            )
            for suggestion_id, text in suggestion_rows
        ),
    )


@dataclass(frozen=True, slots=True)
class SubmittedMessage:
    """A USER message already in an Action: where it went, and what it said."""

    action_id: str
    suggestion_id: str | None
    text: str  # what the task reads, with the attachments it was given
    # Sent while the task was stopped: kept, but never run.
    dropped: bool


def read_submitted_message(*, user_id: str, message_id: str) -> SubmittedMessage | None:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            "SELECT steps.action_id, actions.suggestion_id, steps.user_message_json, "
            "steps.adoption_canceled_at IS NOT NULL "
            "FROM agent_action_steps AS steps JOIN agent_actions AS actions "
            "ON actions.action_id = steps.action_id "
            "WHERE steps.user_id = ? AND steps.user_message_id = ?",
            (user_id, message_id),
        ).fetchone()
    if row is None:
        return None
    action_id, suggestion_id, message_json, dropped = row
    message = parse_action_user_message(str(message_json))
    attached = [
        *(f"image {image.storage_path}" for image in message.images),
        *(f"file {file.name}" for file in message.files),
    ]
    return SubmittedMessage(
        action_id=str(action_id),
        suggestion_id=None if suggestion_id is None else str(suggestion_id),
        text=render_action_user_request_text(message)
        + (f"\n(attached: {', '.join(attached)})" if attached else ""),
        dropped=bool(dropped),
    )


def read_suggestion_texts(*, user_id: str, ids: Sequence[str]) -> dict[str, str]:
    """What each suggestion says to the user, by id."""

    if not ids:
        return {}
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            "SELECT suggestion_id, answer FROM agent_suggestions "
            f"WHERE user_id = ? AND suggestion_id IN ({', '.join('?' * len(ids))})",
            (user_id, *ids),
        ).fetchall()
    return {str(row[0]): str(row[1] or "") for row in rows}


def read_latest_run_process(*, user_id: str, action_id: str) -> str | None:
    """The process of the Action's latest run, which a message to it names.

    A message to a running Action names the run it was written against, and
    one that names none is refused while a run is active.
    """

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            "SELECT adopted_process_id FROM agent_action_steps "
            "WHERE user_id = ? AND action_id = ? AND adopted_process_id IS NOT NULL "
            "ORDER BY accepted_sequence DESC LIMIT 1",
            (user_id, action_id),
        ).fetchone()
    return None if row is None else str(row[0])


def _line(text: str) -> str | None:
    first = next((line.strip() for line in text.splitlines() if line.strip()), None)
    if first is None:
        return None
    return (
        first if len(first) <= _LINE_MAX_CHARS else first[: _LINE_MAX_CHARS - 1] + "…"
    )


__all__ = [
    "CHAT_WORK_LIST_MAX_SUGGESTIONS",
    "CHAT_WORK_LIST_MAX_TASKS",
    "ChatSuggestion",
    "ChatTask",
    "ChatWorkList",
    "SubmittedMessage",
    "read_submitted_message",
    "read_chat_work_list",
    "read_latest_run_process",
    "read_suggestion_texts",
]

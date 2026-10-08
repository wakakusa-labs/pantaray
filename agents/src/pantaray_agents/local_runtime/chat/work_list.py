"""The work a chat turn is shown: its tasks and its open suggestions.

Pantaray is one: every task the user has is its own work, wherever it was
started -- the chat, the Overlay, a shortcut or a suggestion. The list shows
every task still in hand and the latest finished ones, newest first; older
ones are found by searching. The suggestions are the action offers the user
has not reacted to. The ids shown are what a turn routes to, after the items
that named them have left its window.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_db_config,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.schema.action_conversation import ActionStatus
from pantaray_agents.tasks.action_user_message import (
    parse_action_user_message,
    render_action_user_request_text,
)

# The whole list, however many tasks the user has: those in hand first, then the
# latest finished. The rest are found with search_tasks.
CHAT_WORK_LIST_MAX_TASKS: Final[int] = 10
CHAT_WORK_LIST_MAX_SUGGESTIONS: Final[int] = 5
# One line each: enough to tell two tasks apart, short enough for every turn.
_LINE_MAX_CHARS: Final[int] = 120


@dataclass(frozen=True, slots=True)
class ChatTask:
    action_id: str
    status: ActionStatus
    awaiting_approval: bool  # running, and paused on the user's approval
    title: str
    latest: str | None  # the first line of its last answer
    updated_at: str


@dataclass(frozen=True, slots=True)
class ChatSuggestion:
    suggestion_id: str
    title: str


@dataclass(frozen=True, slots=True)
class ChatWorkList:
    tasks: tuple[ChatTask, ...]
    more_tasks: int  # past the cap, for search_tasks to find
    suggestions: tuple[ChatSuggestion, ...]


_IN_HAND = "actions.status IN ('queued', 'processing')"
_TASKS_SQL: Final[str] = """
    SELECT actions.action_id, actions.status,
           EXISTS (
               SELECT 1 FROM processes
               WHERE processes.user_id = actions.user_id
                 AND processes.action_id = actions.action_id
                 AND processes.status = 'paused'
           ),
           actions.final_output, first.user_message_json, actions.updated_at
    FROM agent_actions AS actions
    JOIN agent_action_steps AS first
      ON first.user_id = actions.user_id
     AND first.user_message_id = actions.initial_user_message_id
    WHERE actions.user_id = :user_id AND {where}
    ORDER BY {order} actions.updated_at DESC, actions.action_id
    LIMIT :limit
"""


def read_chat_work_list(*, user_id: str) -> ChatWorkList:
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        tasks = _tasks(
            connection.execute(
                _TASKS_SQL.format(where="1", order=f"{_IN_HAND} DESC,"),
                {"user_id": user_id, "limit": CHAT_WORK_LIST_MAX_TASKS},
            )
        )
        (total,) = connection.execute(
            "SELECT COUNT(*) FROM agent_actions WHERE user_id = ?", (user_id,)
        ).fetchone()
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
        tasks=tasks,
        more_tasks=int(total) - len(tasks),
        suggestions=tuple(
            ChatSuggestion(
                suggestion_id=str(suggestion_id),
                title=_line(text or "") or "(untitled)",
            )
            for suggestion_id, text in suggestion_rows
        ),
    )


def search_tasks(
    *,
    user_id: str,
    query: str | None,
    since: str | None,
    until: str | None,
    limit: int,
) -> tuple[ChatTask, ...]:
    """Every task of the user's whose requests or answer hold ``query``, last
    updated in [``since``, ``until``), newest first.

    Raises ``ValueError`` for a time that is not ISO 8601.
    """

    # Design limit: a scan with LIKE over every task; move to the FTS index when
    # a user's tasks run into the tens of thousands.
    pattern = None if query is None else f"%{_escape_like(query)}%"
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        return _tasks(
            connection.execute(
                _TASKS_SQL.format(
                    where=(
                        "(:pattern IS NULL OR actions.final_output LIKE :pattern "
                        "ESCAPE '\\' OR EXISTS (SELECT 1 FROM agent_action_steps "
                        "AS asked WHERE asked.user_id = actions.user_id "
                        "AND asked.action_id = actions.action_id "
                        "AND asked.step_type = 'user_request' "
                        "AND asked.user_request_text LIKE :pattern ESCAPE '\\')) "
                        "AND (:since IS NULL OR actions.updated_at >= :since) "
                        "AND (:until IS NULL OR actions.updated_at < :until)"
                    ),
                    order="",
                ),
                {
                    "user_id": user_id,
                    "pattern": pattern,
                    "since": None if since is None else _stored_time(since),
                    "until": None if until is None else _stored_time(until),
                    "limit": limit,
                },
            )
        )


def _tasks(rows: Iterable[Sequence[object]]) -> tuple[ChatTask, ...]:
    return tuple(
        ChatTask(
            action_id=str(action_id),
            status=cast(ActionStatus, status),
            awaiting_approval=bool(awaiting_approval),
            title=_line(
                render_action_user_request_text(
                    parse_action_user_message(str(message_json))
                )
            )
            or "(untitled)",
            latest=_line(str(final_output)),
            updated_at=str(updated_at),
        )
        for action_id, status, awaiting_approval, final_output, message_json, updated_at in rows
    )


def _stored_time(value: str) -> str:
    """``value`` as the timestamps are stored, so they compare as strings."""

    # A time without an offset is the user's local time, as the chat shows times.
    return format_utc_iso(datetime.fromisoformat(value).astimezone(UTC))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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


def read_relayed_starts(
    *, user_id: str, patterns: Sequence[str], besides: str
) -> dict[str, frozenset[str]]:
    """The user's messages each task started under ``patterns`` (GLOB over
    message ids), other than ``besides``, relayed, by task."""

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        rows = connection.execute(
            "SELECT steps.action_id, relayed.value FROM agent_action_steps AS steps, "
            "json_each(steps.user_message_json, '$.chat_handoff.relayed_item_ids') "
            "AS relayed WHERE steps.user_id = ? AND steps.user_message_id != ? "
            f"AND ({' OR '.join(['steps.user_message_id GLOB ?'] * len(patterns))})",
            (user_id, besides, *patterns),
        ).fetchall()
    starts: dict[str, set[str]] = {}
    for action_id, item_id in rows:
        starts.setdefault(str(action_id), set()).add(str(item_id))
    return {action_id: frozenset(ids) for action_id, ids in starts.items()}


def read_attachment_holder(*, user_id: str, attachment_id: str) -> str | None:
    """The Action a staged file was handed to; handing it over moved it there."""

    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            "SELECT steps.action_id FROM agent_action_steps AS steps, "
            "json_each(steps.user_message_json, '$.files') AS files "
            "WHERE steps.user_id = ? "
            "AND json_extract(files.value, '$.attachment_id') = ? LIMIT 1",
            (user_id, attachment_id),
        ).fetchone()
    return None if row is None else str(row[0])


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


def answered_after_suggestion(
    *, user_id: str, suggestion_id: str, item_ids: Sequence[str]
) -> bool:
    """Whether one of the user's messages ``item_ids`` was written after the
    suggestion was made, so it can be their answer to it."""

    if not item_ids:
        return False
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, busy_timeout_ms)
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM chat_items WHERE user_id = ? "
            "AND kind = 'user_message' "
            f"AND item_id IN ({', '.join('?' * len(item_ids))}) "
            "AND created_at > COALESCE((SELECT created_at FROM agent_suggestions "
            "WHERE user_id = ? AND suggestion_id = ?), ''))",
            (user_id, *item_ids, user_id, suggestion_id),
        ).fetchone()
    return bool(row[0])


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
    "read_attachment_holder",
    "read_chat_work_list",
    "search_tasks",
    "read_latest_run_process",
    "read_suggestion_texts",
]

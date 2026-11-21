"""Which dismissed suggestions the next unified Memory run is handed.

A suggestion the user dismissed without replying reaches memory nowhere else:
an accepted one, and a dismissal the user answered in words, become Actions.
The dismissals are a source of their own, handed as the chat is: each is an
event in the Memory update hook's inbox, a run is handed those still
dismissed without a reply, pinned in its job payload by suggestion id, and
their events are consumed when that job is enqueued.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final

from .job_payload_models import MEMORY_UPDATE_SOURCE_MAX_ITEMS

# Design limit: as with the chat, one run reads at most this many characters of
# suggestion text and reasons, and a longer backlog waits for the next runs.
# Raise it if dismissal backlogs are seen to lag behind by several runs.
MEMORY_DISMISSAL_MAX_CHARS: Final[int] = 60_000

# A reply after a dismissal is recorded as an Action step that names the
# suggestion, and that Action reaches memory on its own.
_DISMISSED_WITHOUT_REPLY_SQL: Final[str] = """
    suggestions.user_id = ? AND suggestions.user_reaction = 'rejected'
    AND NOT EXISTS (
        SELECT 1 FROM agent_action_steps AS reply
        WHERE reply.user_id = suggestions.user_id
          AND reply.source_suggestion_id = suggestions.suggestion_id
    )
"""


@dataclass(frozen=True, slots=True)
class DismissedSuggestion:
    suggestion_id: str
    rejected_at: str
    answer: str
    dismiss_reason: str | None


def is_dismissed_without_reply(
    connection: sqlite3.Connection, *, user_id: str, suggestion_id: str
) -> bool:
    row = connection.execute(
        f"""
        SELECT 1 FROM agent_suggestions AS suggestions
        WHERE {_DISMISSED_WITHOUT_REPLY_SQL} AND suggestions.suggestion_id = ?
        """,
        (user_id, suggestion_id),
    ).fetchone()
    return row is not None


def dismissals_for_one_run(
    connection: sqlite3.Connection, *, user_id: str, suggestion_ids: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Of the pending dismissals still unanswered, those that fit one run and the
    rest, oldest dismissal first.

    A dismissal longer than the character bound still goes alone, so the
    backlog always moves.
    """

    placeholders = ",".join("?" for _ in suggestion_ids)
    rows = connection.execute(
        f"""
        SELECT suggestions.suggestion_id,
               LENGTH(suggestions.answer)
                   + COALESCE(LENGTH(suggestions.dismiss_reason), 0)
        FROM agent_suggestions AS suggestions
        WHERE {_DISMISSED_WITHOUT_REPLY_SQL}
          AND suggestions.suggestion_id IN ({placeholders})
        ORDER BY suggestions.rejected_at, suggestions.suggestion_id
        """,
        (user_id, *suggestion_ids),
    ).fetchall()
    handed = [str(row[0]) for row in rows[:1]]
    total = sum(int(row[1]) for row in rows[:1])
    for row in rows[1:MEMORY_UPDATE_SOURCE_MAX_ITEMS]:
        total += int(row[1])
        if total > MEMORY_DISMISSAL_MAX_CHARS:
            break
        handed.append(str(row[0]))
    rest = tuple(str(row[0]) for row in rows[len(handed) :])
    return tuple(handed), rest


def read_dismissed_suggestions(
    connection: sqlite3.Connection, *, user_id: str, suggestion_ids: tuple[str, ...]
) -> tuple[DismissedSuggestion, ...]:
    """The pinned dismissals still unanswered, oldest first."""

    placeholders = ",".join("?" for _ in suggestion_ids)
    rows = connection.execute(
        f"""
        SELECT suggestions.suggestion_id, suggestions.rejected_at,
               suggestions.answer, suggestions.dismiss_reason
        FROM agent_suggestions AS suggestions
        WHERE {_DISMISSED_WITHOUT_REPLY_SQL}
          AND suggestions.suggestion_id IN ({placeholders})
        ORDER BY suggestions.rejected_at, suggestions.suggestion_id
        """,
        (user_id, *suggestion_ids),
    ).fetchall()
    return tuple(
        DismissedSuggestion(
            suggestion_id=str(row[0]),
            rejected_at=str(row[1]),
            answer=str(row[2]),
            dismiss_reason=None if row[3] is None else str(row[3]),
        )
        for row in rows
    )


__all__ = [
    "MEMORY_DISMISSAL_MAX_CHARS",
    "DismissedSuggestion",
    "dismissals_for_one_run",
    "is_dismissed_without_reply",
    "read_dismissed_suggestions",
]

"""Which dismissed suggestions the next unified Memory run is handed.

A suggestion the user dismissed without replying reaches memory nowhere else:
an accepted one, and a dismissal the user answered in words, become Actions.
The dismissals are a source of their own, handed as the chat is: a run is
handed the dismissals after the user's cursor, pinned in its job payload by
suggestion id, and the cursor moves to the last of them when that job is
enqueued. A failed run therefore loses its dismissals as it loses its triggers.
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
class PendingDismissals:
    suggestion_ids: tuple[str, ...]
    # When the oldest of them was dismissed.
    oldest_rejected_at: str
    through_rejected_at: str
    through_suggestion_id: str


@dataclass(frozen=True, slots=True)
class DismissedSuggestion:
    suggestion_id: str
    rejected_at: str
    answer: str
    dismiss_reason: str | None


def pending_dismissals(
    connection: sqlite3.Connection, *, user_id: str
) -> PendingDismissals | None:
    """The dismissals after the cursor that fit one run, or None when none wait.

    A dismissal longer than the character bound still goes alone, so the
    cursor always moves.
    """

    cursor = connection.execute(
        """
        SELECT through_rejected_at, through_suggestion_id
        FROM memory_dismissal_cursors WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    after = ("", "") if cursor is None else (str(cursor[0]), str(cursor[1]))
    rows = connection.execute(
        f"""
        SELECT suggestions.suggestion_id, suggestions.rejected_at,
               LENGTH(suggestions.answer)
                   + COALESCE(LENGTH(suggestions.dismiss_reason), 0)
        FROM agent_suggestions AS suggestions
        WHERE {_DISMISSED_WITHOUT_REPLY_SQL}
          AND (suggestions.rejected_at, suggestions.suggestion_id) > (?, ?)
        ORDER BY suggestions.rejected_at, suggestions.suggestion_id
        LIMIT ?
        """,
        (user_id, *after, MEMORY_UPDATE_SOURCE_MAX_ITEMS),
    ).fetchall()
    if not rows:
        return None
    handed = rows[:1]
    total = int(rows[0][2])
    for row in rows[1:]:
        total += int(row[2])
        if total > MEMORY_DISMISSAL_MAX_CHARS:
            break
        handed.append(row)
    return PendingDismissals(
        suggestion_ids=tuple(str(row[0]) for row in handed),
        oldest_rejected_at=str(handed[0][1]),
        through_rejected_at=str(handed[-1][1]),
        through_suggestion_id=str(handed[-1][0]),
    )


def advance_dismissal_cursor(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    dismissals: PendingDismissals,
    at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO memory_dismissal_cursors(
            user_id, through_rejected_at, through_suggestion_id, updated_at
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            through_rejected_at = excluded.through_rejected_at,
            through_suggestion_id = excluded.through_suggestion_id,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            dismissals.through_rejected_at,
            dismissals.through_suggestion_id,
            at,
        ),
    )


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
    "PendingDismissals",
    "advance_dismissal_cursor",
    "pending_dismissals",
    "read_dismissed_suggestions",
]

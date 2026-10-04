"""The message that greets an owner whose recording starts with no data yet.

A first Suggestion needs recorded activity, so a new user would otherwise see
nothing for the first several minutes. This message says Pantaray is learning
their work and can take a request at any time. It is stored as an ordinary
finished `message_only` Suggestion without running the SuggestionAgent, so the
relay shows it, History lists it, and a reply continues it like any other
Suggestion. Like any Suggestion it is only stored while a session can show it;
the route refuses it otherwise, and the desktop app asks again.

Whether to greet is decided here, from the owner's rows, not by the desktop
app: settings files follow a guest into a new account while these rows do not,
and a user who upgrades already has them.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Final

from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

WELCOME_SUGGESTION_PROMPT_NAME: Final[str] = "onboarding_welcome"
WELCOME_SUGGESTION_PROMPT_VERSION: Final[str] = "1"
WELCOME_SUGGESTION_MAX_CHARS: Final[int] = 2_000

# uuidv5(uuid.NAMESPACE_URL, "pantaray:welcome_suggestion:v1")
_NAMESPACE_WELCOME_SUGGESTION: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:welcome_suggestion:v1"
)


def _derived_id(kind: str, *, user_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE_WELCOME_SUGGESTION, f"{kind}:{user_id}"))


def welcome_suggestion_id(user_id: str) -> str:
    return _derived_id("suggestion", user_id=user_id)


def record_welcome_suggestion(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    answer: str,
    now: str,
) -> bool:
    """Store the welcome as a finished Suggestion; False when the owner has data.

    Any Suggestion (the welcome included), activity log or Action means the
    owner is not new, so a repeated request never greets twice. The process is
    written already completed at `now`: the relay delivers a process that
    finished after its session started, and nothing runs for it.
    """
    suggestion_id = welcome_suggestion_id(user_id)
    with immediate_transaction(connection):
        has_data = connection.execute(
            """
            SELECT EXISTS (SELECT 1 FROM agent_suggestions WHERE user_id = :user_id)
                OR EXISTS (SELECT 1 FROM activity_logs WHERE user_id = :user_id)
                OR EXISTS (SELECT 1 FROM agent_actions WHERE user_id = :user_id)
            """,
            {"user_id": user_id},
        ).fetchone()[0]
        if has_data:
            return False
        # Released, not held: the welcome is shown at once.
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, answer, prompt_name,
                prompt_version, has_suggestion, interaction_contract,
                delivery_state, created_at, updated_at
            ) VALUES (?, ?, 'success', ?, ?, ?, 1, 'message_only', 'released', ?, ?)
            """,
            (
                suggestion_id,
                user_id,
                answer,
                WELCOME_SUGGESTION_PROMPT_NAME,
                WELCOME_SUGGESTION_PROMPT_VERSION,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, suggestion_id,
                started_at, updated_at, completed_at, heartbeat_at, next_event_seq
            ) VALUES (?, ?, 'suggestion', 'completed', ?, ?, ?, ?, ?, 1)
            """,
            (
                _derived_id("process", user_id=user_id),
                user_id,
                suggestion_id,
                now,
                now,
                now,
                now,
            ),
        )
    return True

"""Show or end the Suggestion an owner holds.

A stored Suggestion is held instead of shown at once, so the moment it reaches
the user can be chosen. A periodic task decides what becomes of it: one that
waited too long, that recording was turned off behind, or that no session can
show now ends unshown (`expired`); any other is released now. Only a released
Suggestion is shown by the relay, listed in History and remembered.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.memory_catalog.domain_registration import (
    register_inline_domain_memory,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .utc_timestamps import format_utc_iso, parse_utc_iso

SUGGESTION_RELEASE_INTERVAL_SECONDS: Final[float] = 30.0
# Two short-Insight windows. An older Suggestion is stale, and the next review
# can make a fresh one from newer activity.
SUGGESTION_HOLD_LIMIT: Final[timedelta] = timedelta(minutes=30)

type HeldSuggestionOutcome = Literal["released", "expired"]


def held_suggestion_outcome(
    *,
    held_since: datetime,
    now: datetime,
    capture_paused: bool,
    session_can_show: bool,
) -> HeldSuggestionOutcome:
    """Whether a held Suggestion is shown now or ends unshown.

    Recording turned off and no session to show it are the rules the Suggestion
    job applies before it stores one: a Suggestion is only useful when it is
    made, so nothing shows it later.
    """
    if (
        capture_paused
        or not session_can_show
        or now - held_since >= SUGGESTION_HOLD_LIMIT
    ):
        return "expired"
    return "released"


def release_held_suggestion(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    now: datetime,
    session_can_show: bool,
) -> HeldSuggestionOutcome | None:
    """Release or expire the owner's held Suggestion; None when none is held.

    `updated_at` marks when it was held, and becomes when it was released, so
    History orders it by the moment it was shown. The text enters Memory only
    once shown: an unshown Suggestion is not something Pantaray said.
    """
    with (
        open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms
        ) as connection,
        immediate_transaction(connection),
    ):
        held = connection.execute(
            """
            SELECT suggestion_id, answer, updated_at FROM agent_suggestions
            WHERE user_id = ? AND delivery_state = 'held'
            """,
            (user_id,),
        ).fetchone()
        if held is None:
            return None
        outcome = held_suggestion_outcome(
            held_since=parse_utc_iso(str(held["updated_at"])),
            now=now,
            capture_paused=store.is_capture_paused(connection, user_id),
            session_can_show=session_can_show,
        )
        connection.execute(
            """
            UPDATE agent_suggestions SET delivery_state = ?, updated_at = ?
            WHERE user_id = ? AND suggestion_id = ?
            """,
            (outcome, format_utc_iso(now), user_id, held["suggestion_id"]),
        )
        if outcome == "released":
            register_inline_domain_memory(
                connection=connection,
                user_id=user_id,
                source="suggestion",
                source_record_id=str(held["suggestion_id"]),
                content=str(held["answer"]),
            )
    return outcome

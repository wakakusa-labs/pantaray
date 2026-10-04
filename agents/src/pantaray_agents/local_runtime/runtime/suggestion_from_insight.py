"""Suggestion inputs queued after their unified Memory update completes."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.tasks.types import SuggestionJobPayload

from .insight_queue import SHORT_INSIGHT_WINDOW_SECONDS
from .job_enqueue import LocalJobEnqueueResult, enqueue_local_job
from .job_payload_models import parse_suggestion_job_payload_json
from .job_types import LOCAL_SUGGESTION_JOB_TYPE
from .process_events import append_process_event_in_connection
from .suggestion_queue import build_local_suggestion_enqueue_request
from .utc_timestamps import format_utc_iso, parse_utc_iso

# uuidv5(uuid.NAMESPACE_URL, "pantaray:suggestion_from_insight:v1")
NAMESPACE_SUGGESTION_FROM_INSIGHT: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:suggestion_from_insight:v1"
)


class MissingReconsideredInsight(LookupError):
    """The Suggestion job outlived the short Insight that asked for it."""


@dataclass(frozen=True, slots=True)
class ReconsideredInsight:
    """The whole short-term Insight plus the reason it asks to reconsider."""

    short_term_insight: str
    reconsideration_reason: str
    # The context-read cursor committed with this Insight; None for a row
    # written before the cursor was stored.
    source_cursor: str | None


def _derived_id(kind: str, *, insight_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE_SUGGESTION_FROM_INSIGHT, f"{kind}:{insight_id}"))


def suggestion_id_for_insight(insight_id: str) -> str:
    return _derived_id("suggestion", insight_id=insight_id)


def enqueue_suggestion_for_insight(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    insight_id: str,
    now: str,
) -> LocalJobEnqueueResult:
    """Queue one Suggestion inside the caller's Memory completion transaction.

    Every identity is derived from `insight_id`, so a replayed Insight run
    neither queues a second job nor creates a second Suggestion row.
    """
    suggestion_id = suggestion_id_for_insight(insight_id)
    job_id = _derived_id("job", insight_id=insight_id)
    existing = connection.execute(
        "SELECT payload_json FROM job_payloads WHERE job_id = ?", (job_id,)
    ).fetchone()
    # Replaying a completion retains the original immutable enqueue timestamp.
    enqueued_at = (
        parse_suggestion_job_payload_json(str(existing[0]))["enqueued_at"]
        if existing is not None
        else now
    )
    payload: SuggestionJobPayload = {
        "job_id": job_id,
        "process_id": _derived_id("process", insight_id=insight_id),
        "suggestion_id": suggestion_id,
        "user_id": user_id,
        "insight_id": insight_id,
        "enqueued_at": enqueued_at,
    }
    result = enqueue_local_job(
        connection=connection,
        request=build_local_suggestion_enqueue_request(payload),
    )
    connection.execute(
        """
        INSERT INTO agent_suggestions(
            suggestion_id, user_id, status, created_at, updated_at
        ) VALUES (?, ?, 'processing', ?, ?)
        ON CONFLICT(suggestion_id) DO NOTHING
        """,
        (suggestion_id, user_id, now, now),
    )
    return result


def capture_paused_for_user(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> bool:
    """Whether recording is off for this user, read fresh from the source store."""
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        return store.is_capture_paused(connection, user_id)


def read_reconsidered_insight(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    insight_id: str,
) -> ReconsideredInsight:
    """Load the Insight; an unchanged situation still permits a periodic review."""
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        row = connection.execute(
            """
            SELECT short_term_insight_data, reconsideration_reason, source_cursor
            FROM agent_insights
            WHERE user_id = ? AND insight_id = ?
            """,
            (user_id, insight_id),
        ).fetchone()
    if row is None:
        raise MissingReconsideredInsight(f"short Insight not found: {insight_id}")
    insight, reason, cursor = row[0], row[1], row[2]
    if not insight:
        raise MissingReconsideredInsight(f"short Insight is empty: {insight_id}")
    return ReconsideredInsight(
        short_term_insight=str(insight),
        reconsideration_reason=(
            str(reason)
            if reason
            else "Periodic review of pending work and long-term direction after Memory updated."
        ),
        source_cursor=None if cursor is None else str(cursor),
    )


def reserve_suggestion_start(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    job_id: str,
    process_id: str,
    now: datetime,
) -> Literal["start", "superseded"] | datetime:
    """Coalesce queued reviews and reserve one generation start per 15 minutes.

    A claim is not a generation: preflight can fail or drop the job. Record the
    start only when the worker is about to enter the agent, and count retries
    too. The existing process event survives restarts and the transaction keeps
    two workers from reserving the same interval.
    """
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        with immediate_transaction(connection):
            latest = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE user_id = ? AND job_type = ?
                ORDER BY rowid DESC LIMIT 1
                """,
                (user_id, LOCAL_SUGGESTION_JOB_TYPE),
            ).fetchone()
            if latest is not None and latest[0] != job_id:
                return "superseded"
            last_start = connection.execute(
                """
                SELECT events.created_at FROM process_events AS events
                JOIN processes ON processes.process_id = events.process_id
                WHERE processes.user_id = ?
                  AND events.event_name = 'suggestion_generation_started'
                ORDER BY events.rowid DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if last_start is not None:
                next_start = parse_utc_iso(str(last_start[0])) + timedelta(
                    seconds=SHORT_INSIGHT_WINDOW_SECONDS
                )
                if now < next_start:
                    return next_start
            append_process_event_in_connection(
                connection=connection,
                process_id=process_id,
                event_name="suggestion_generation_started",
                payload={},
                created_at=format_utc_iso(now),
            )
    return "start"

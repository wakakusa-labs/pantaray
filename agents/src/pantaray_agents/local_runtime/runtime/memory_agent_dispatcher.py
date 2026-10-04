from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.tasks.types import MemoryUpdateActionTerminal
from pantaray_agents.utils.structured_logging import log_structured_event

from .insight_queue import SHORT_INSIGHT_WINDOW_SECONDS
from .job_payload_models import MEMORY_UPDATE_SOURCE_MAX_ITEMS
from .job_status import ACTIVE_DEDUPE_JOB_STATUSES
from .job_types import LOCAL_MEMORY_UPDATE_JOB_TYPE
from .memory_agent_triggers import (
    ACTION_TERMINAL_MEMORY_TRIGGER_KIND,
    UNIFIED_MEMORY_TRIGGER_KINDS,
    MemoryAgentTrigger,
    MemoryAgentTriggerIntegrityError,
    MemoryAgentTriggerKind,
)
from .memory_update_queue import (
    PendingMemoryTrigger,
    build_coalesced_memory_update_payload,
    enqueue_memory_update_job_in_connection,
)

logger = logging.getLogger(__name__)

_TRIGGER_STATUS_PENDING: Final[str] = "pending"
_TRIGGER_STATUS_DISPATCHED: Final[str] = "dispatched"
_JOB_ENQUEUED: Final[str] = "JOB_ENQUEUED"
MEMORY_AGENT_DISPATCH_INTERVAL_SECONDS: Final[float] = 30.0

MEMORY_UPDATE_MAX_COALESCED_TRIGGERS: Final[int] = MEMORY_UPDATE_SOURCE_MAX_ITEMS
# Finished Action turns ride along with the next Insight or summary run instead
# of starting one each. One Insight window bounds the wait, which matters when
# recording is off and no Insight run comes.
ACTION_TERMINAL_MAX_DEFERRAL: Final[timedelta] = timedelta(
    seconds=SHORT_INSIGHT_WINDOW_SECONDS
)


@dataclass(frozen=True, slots=True)
class MemoryAgentDispatchOutcome:
    """One pending trigger this poll bound to a unified Memory run."""

    trigger: MemoryAgentTrigger
    job_id: str


@dataclass(frozen=True, slots=True)
class MemoryAgentDispatchResult:
    outcomes: tuple[MemoryAgentDispatchOutcome, ...]


def dispatch_memory_agent_triggers_once(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    now: datetime | None = None,
) -> MemoryAgentDispatchResult:
    if not user_id.strip():
        raise ValueError("user_id must not be empty")
    current = datetime.now(UTC) if now is None else now
    handled_at = format_utc_iso(current)
    with open_memory_catalog_connection(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            outcomes = _dispatch_coalesced_memory_update(
                connection=connection,
                user_id=user_id,
                now=current,
                handled_at=handled_at,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    result = MemoryAgentDispatchResult(outcomes=outcomes)
    _log_dispatch_result(result)
    return result


def _dispatch_coalesced_memory_update(
    *,
    connection: sqlite3.Connection,
    user_id: str,
    now: datetime,
    handled_at: str,
) -> tuple[MemoryAgentDispatchOutcome, ...]:
    if _has_active_memory_update_job(connection=connection, user_id=user_id):
        return ()
    pending = _select_pending_unified_triggers(connection=connection, user_id=user_id)
    if not _is_due(pending, now=now):
        return ()
    payload = build_coalesced_memory_update_payload(
        user_id=user_id,
        enqueued_at=handled_at,
        pending=pending,
    )
    job_id = enqueue_memory_update_job_in_connection(
        connection=connection, payload=payload
    )
    return tuple(
        _mark_dispatched(connection, entry.trigger, job_id, handled_at)
        for entry in pending
    )


def _is_due(pending: tuple[PendingMemoryTrigger, ...], *, now: datetime) -> bool:
    if not pending:
        return False
    if any(
        entry.trigger.trigger_kind != ACTION_TERMINAL_MEMORY_TRIGGER_KIND
        for entry in pending
    ):
        return True
    oldest = min(datetime.fromisoformat(entry.created_at) for entry in pending)
    return now - oldest >= ACTION_TERMINAL_MAX_DEFERRAL


def _select_pending_unified_triggers(
    *, connection: sqlite3.Connection, user_id: str
) -> tuple[PendingMemoryTrigger, ...]:
    placeholders = ",".join("?" for _ in UNIFIED_MEMORY_TRIGGER_KINDS)
    rows = connection.execute(
        f"""
        SELECT
            user_id, trigger_kind, source_id, created_at, action_id,
            action_completed_at, source_action_revision_id,
            turn_start_step_number, turn_end_step_number, action_prompt_name,
            action_prompt_version, suggestion_id
        FROM memory_agent_triggers
        WHERE user_id = ? AND status = ? AND trigger_kind IN ({placeholders})
        ORDER BY created_at ASC, trigger_kind ASC, source_id ASC
        LIMIT ?
        """,
        (
            user_id,
            _TRIGGER_STATUS_PENDING,
            *UNIFIED_MEMORY_TRIGGER_KINDS,
            MEMORY_UPDATE_MAX_COALESCED_TRIGGERS,
        ),
    ).fetchall()
    return tuple(_pending_trigger_from_row(row) for row in rows)


def _pending_trigger_from_row(row: sqlite3.Row) -> PendingMemoryTrigger:
    trigger_kind = cast(MemoryAgentTriggerKind, str(row["trigger_kind"]))
    trigger = MemoryAgentTrigger(
        user_id=str(row["user_id"]),
        trigger_kind=trigger_kind,
        source_id=str(row["source_id"]),
    )
    action_terminal: MemoryUpdateActionTerminal | None = None
    if trigger_kind == ACTION_TERMINAL_MEMORY_TRIGGER_KIND:
        action_terminal = {
            "source_id": trigger.source_id,
            "action_id": str(row["action_id"]),
            "action_completed_at": str(row["action_completed_at"]),
            "turn_start_step_number": int(row["turn_start_step_number"]),
            "turn_end_step_number": int(row["turn_end_step_number"]),
            "action_prompt_name": str(row["action_prompt_name"]),
            "action_prompt_version": str(row["action_prompt_version"]),
        }
        if row["source_action_revision_id"] is not None:
            action_terminal["source_action_revision_id"] = str(
                row["source_action_revision_id"]
            )
        if row["suggestion_id"] is not None:
            action_terminal["suggestion_id"] = str(row["suggestion_id"])
    return PendingMemoryTrigger(
        trigger=trigger,
        created_at=str(row["created_at"]),
        action_terminal=action_terminal,
    )


def _has_active_memory_update_job(
    *, connection: sqlite3.Connection, user_id: str
) -> bool:
    status_placeholders = ",".join("?" for _ in ACTIVE_DEDUPE_JOB_STATUSES)
    row = connection.execute(
        f"""
        SELECT 1
        FROM jobs
        WHERE user_id = ? AND job_type = ?
          AND status IN ({status_placeholders})
        LIMIT 1
        """,
        (user_id, LOCAL_MEMORY_UPDATE_JOB_TYPE, *ACTIVE_DEDUPE_JOB_STATUSES),
    ).fetchone()
    return row is not None


def _mark_dispatched(
    connection: sqlite3.Connection,
    trigger: MemoryAgentTrigger,
    job_id: str,
    handled_at: str,
) -> MemoryAgentDispatchOutcome:
    cursor = connection.execute(
        """
        UPDATE memory_agent_triggers
        SET status = ?, dispatched_job_id = ?, outcome_code = ?, handled_at = ?
        WHERE user_id = ? AND trigger_kind = ? AND source_id = ? AND status = ?
        """,
        (
            _TRIGGER_STATUS_DISPATCHED,
            job_id,
            _JOB_ENQUEUED,
            handled_at,
            trigger.user_id,
            trigger.trigger_kind,
            trigger.source_id,
            _TRIGGER_STATUS_PENDING,
        ),
    )
    if cursor.rowcount != 1:
        raise MemoryAgentTriggerIntegrityError(
            "pending Memory Agent trigger ownership was lost"
        )
    return MemoryAgentDispatchOutcome(trigger=trigger, job_id=job_id)


def _log_dispatch_result(result: MemoryAgentDispatchResult) -> None:
    for outcome in result.outcomes:
        log_structured_event(
            logger,
            level="info",
            evt="MEMORY_AGENT_TRIGGER_HANDLED",
            component="local_runtime.memory_agent_dispatcher",
            user_id=outcome.trigger.user_id,
            trigger_kind=outcome.trigger.trigger_kind,
            source_id=outcome.trigger.source_id,
            job_id=outcome.job_id,
            outcome_code=_JOB_ENQUEUED,
        )


__all__ = [
    "MemoryAgentDispatchOutcome",
    "MemoryAgentDispatchResult",
    "dispatch_memory_agent_triggers_once",
]

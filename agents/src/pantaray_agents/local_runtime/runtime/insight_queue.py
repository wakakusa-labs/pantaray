"""Run identity and enqueue for the periodic short Insight over raw Zanei."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pantaray_agents.local_runtime.activity_summary_schedule import SummaryType, iso_z
from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.tasks.types import InsightJobPayload

from .job_enqueue import (
    LocalJobEnqueueRequest,
    LocalJobEnqueueResult,
    enqueue_local_job_with_connection,
)
from .job_payload_models import parse_insight_job_payload_json
from .job_status import FINALIZABLE_JOB_STATUSES, PROCESS_STATUS_ENQUEUED
from .job_types import INSIGHT_PROCESS_KIND, LOCAL_INSIGHT_JOB_TYPE

# Activity periods stay inside one clock hour so the 1h Activity Summary, which
# selects logs by `period_start >= window_start AND period_end <= window_end`,
# neither drops nor double counts a run. 3600 is a multiple of this window and
# the epoch is hour-aligned, so an epoch-aligned window never crosses an hour.
SHORT_INSIGHT_WINDOW_SECONDS: Final[int] = 900

# The 1h summary is the only one built from `activity_logs`; the longer windows
# aggregate summaries instead.
SHORT_INSIGHT_ACTIVITY_SUMMARY_TYPE: Final[SummaryType] = "1h"

# A new identity prevents a 15-minute payload colliding with a persisted
# 4-minute job at the same window start during an application upgrade.
NAMESPACE_SHORT_INSIGHT: Final[uuid.UUID] = uuid.uuid5(
    uuid.NAMESPACE_URL, "pantaray:short_insight:v2"
)


def short_insight_window_start(now: datetime) -> datetime:
    epoch_seconds = int(now.astimezone(UTC).timestamp())
    aligned = epoch_seconds - epoch_seconds % SHORT_INSIGHT_WINDOW_SECONDS
    return datetime.fromtimestamp(aligned, UTC)


def _window_id(kind: str, *, user_id: str, window_start: str) -> str:
    return str(uuid.uuid5(NAMESPACE_SHORT_INSIGHT, f"{kind}:{user_id}:{window_start}"))


def build_short_insight_job_payload(
    *, user_id: str, window_start: datetime
) -> InsightJobPayload:
    """Derive one run per (user, window) so a repeated enqueue stays a no-op.

    Every field is derived from the window because `enqueue_local_job` compares
    the whole stored payload when a job id already exists.
    """
    period_start = iso_z(window_start)
    return {
        "job_id": _window_id("job", user_id=user_id, window_start=period_start),
        "process_id": _window_id("process", user_id=user_id, window_start=period_start),
        "insight_id": _window_id("insight", user_id=user_id, window_start=period_start),
        "user_id": user_id,
        "period_start": period_start,
        "period_end": iso_z(
            window_start + timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS)
        ),
        "enqueued_at": period_start,
    }


def build_local_insight_enqueue_request(
    payload: InsightJobPayload,
) -> LocalJobEnqueueRequest:
    return {
        "job_id": payload["job_id"],
        "user_id": payload["user_id"],
        "job_type": LOCAL_INSIGHT_JOB_TYPE,
        "process_id": payload["process_id"],
        "process_kind": INSIGHT_PROCESS_KIND,
        "process_status": PROCESS_STATUS_ENQUEUED,
        "scheduled_at": payload["enqueued_at"],
        # One active short Insight per user; a later window waits for the run.
        "logical_key": payload["user_id"],
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "process_started_at": payload["enqueued_at"],
        "process_updated_at": payload["enqueued_at"],
        "process_heartbeat_at": payload["enqueued_at"],
        "process_next_event_seq": 1,
        "process_suggestion_id": None,
    }


async def enqueue_due_short_insight_job(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
    now: datetime | None = None,
) -> LocalJobEnqueueResult | None:
    """Enqueue this window's run while the user records with a current source.

    Paused capture records nothing new, and this run is the only Suggestion
    trigger (`suggestion_from_insight`), so skipping it is what stops suggestions
    while recording is off: the tail recorded before the user turned it off must
    not become a suggestion after.
    """
    async with context_source_control.gate.turn():
        if context_source_control.gate.current(user_id) is None:
            return None
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms
    ) as connection:
        source = store.get_source(connection, user_id)
    if source is None or source.kind != "ready" or source.capture_paused:
        return None
    payload = build_short_insight_job_payload(
        user_id=user_id,
        window_start=short_insight_window_start(
            datetime.now(UTC) if now is None else now
        ),
    )
    return enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=busy_timeout_ms,
        request=build_local_insight_enqueue_request(payload),
    )


def has_unfinished_short_insight_run(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    window_start: str,
    window_end: str,
) -> bool:
    """Report whether a window still owes `activity_logs` rows to a summary.

    A run may take minutes, so a window can outlive the summary grace period. The
    summary would then commit an incomplete hour and advance its cursor past it,
    dropping the late rows for good.
    """
    rows = connection.execute(
        f"""SELECT job_payloads.payload_json
            FROM jobs JOIN job_payloads ON job_payloads.job_id = jobs.job_id
            WHERE jobs.user_id = ? AND jobs.job_type = ?
              AND jobs.status NOT IN
                  ({",".join("?" for _ in FINALIZABLE_JOB_STATUSES)})""",
        (user_id, LOCAL_INSIGHT_JOB_TYPE, *FINALIZABLE_JOB_STATUSES),
    ).fetchall()
    return any(
        window_start <= payload["period_start"] and payload["period_end"] <= window_end
        for payload in (parse_insight_job_payload_json(str(row[0])) for row in rows)
    )

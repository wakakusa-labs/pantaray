from __future__ import annotations

import sqlite3
import uuid
from typing import TypedDict

from pantaray_agents.local_runtime.runtime.activity_queue import (
    enqueue_local_activity_summary_job_in_connection,
)
from pantaray_agents.local_runtime.runtime.activity_source_rows import (
    reserve_activity_summary_processing_in_connection,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.job_payload_builder import (
    build_activity_summary_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_activity_summary_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.job_reads import read_local_job_payload_json
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction


class ActivitySummaryEnqueueResult(TypedDict):
    job_id: str
    process_id: str
    summary_id: str


def enqueue_activity_summary_job(
    *,
    user_id: str,
    summary_id: str,
    summary_type: str,
    period_start: str,
    period_end: str,
) -> ActivitySummaryEnqueueResult:
    process_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    enqueued_at = now_utc_iso()
    db_path, timeout_ms = read_local_runtime_db_config()
    payload = build_activity_summary_job_payload(
        {
            "job_id": job_id,
            "process_id": process_id,
            "summary_id": summary_id,
            "user_id": user_id,
            "enqueued_at": enqueued_at,
            "summary_type": summary_type,
            "period_start": period_start,
            "period_end": period_end,
        }
    )
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection, timeout_ms)
        with immediate_transaction(connection):
            reserve_activity_summary_processing_in_connection(
                connection=connection,
                summary_id=summary_id,
                user_id=user_id,
                summary_type=summary_type,
                period_start=period_start,
                period_end=period_end,
                created_at=enqueued_at,
            )
            result = enqueue_local_activity_summary_job_in_connection(
                connection=connection,
                payload=payload,
            )
    if result["inserted_new"]:
        return {
            "job_id": result["job_id"],
            "process_id": result["process_id"],
            "summary_id": payload["summary_id"],
        }
    existing_payload_json = read_local_job_payload_json(
        db_path=str(db_path),
        busy_timeout_ms=timeout_ms,
        job_id=result["job_id"],
    )
    if existing_payload_json is None:
        raise RuntimeError("Existing activity-summary job payload not found")
    existing_payload = parse_activity_summary_job_payload_json(existing_payload_json)
    return {
        "job_id": result["job_id"],
        "process_id": result["process_id"],
        "summary_id": existing_payload["summary_id"],
    }

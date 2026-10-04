from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.job_claim import (
    claim_next_pending_job,
    finalize_local_job,
)
from pantaray_agents.local_runtime.runtime.job_types import LOCAL_SUGGESTION_JOB_TYPE
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    enqueue_suggestion_for_insight,
    read_reconsidered_insight,
    reserve_suggestion_start,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .migrated_db import prepare_test_database

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "INSERT INTO users(user_id,ui_language,created_at,updated_at) VALUES (?,'ja',?,?)",
            [(user, NOW.isoformat(), NOW.isoformat()) for user in ("user-1", "user-2")],
        )
    return db_path


def _queue(
    db_path: Path, insight_id: str, user_id: str = "user-1", *, at: datetime = NOW
) -> None:
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        with immediate_transaction(conn):
            enqueue_suggestion_for_insight(
                connection=conn,
                user_id=user_id,
                insight_id=insight_id,
                now=at.isoformat(),
            )


def _claim(db_path: Path, user_id: str = "user-1") -> dict[str, str]:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=LOCAL_SUGGESTION_JOB_TYPE,
        owner_user_id=user_id,
        claimed_by="worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    return json.loads(claimed["payload_json"])


def _reserve(db_path: Path, payload: dict[str, str], now: datetime):
    return reserve_suggestion_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=payload["user_id"],
        job_id=payload["job_id"],
        process_id=payload["process_id"],
        now=now,
    )


def test_generation_starts_stay_fifteen_minutes_apart_after_reopening_db(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    _queue(db_path, "insight-1")
    first = _claim(db_path)
    assert _reserve(db_path, first, NOW) == "start"
    finalize_local_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_id=first["job_id"],
        final_status="completed",
        process_final_status="completed",
    )
    _queue(db_path, "insight-2")
    second = _claim(db_path)
    # Each call opens a fresh connection; no in-memory timer owns the interval.
    due = NOW + timedelta(minutes=15)
    assert _reserve(db_path, second, due - timedelta(seconds=1)) == due
    assert _reserve(db_path, second, due) == "start"
    # A retry of the same job must also respect its previous generation start.
    assert _reserve(db_path, second, due + timedelta(seconds=1)) == due + timedelta(
        minutes=15
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM process_events WHERE event_name='suggestion_generation_started'"
        ).fetchone() == (2,)


def test_only_latest_queued_review_reserves_a_start(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _queue(db_path, "older")
    _queue(db_path, "latest", at=NOW + timedelta(seconds=1))
    old = _claim(db_path)
    assert _reserve(db_path, old, NOW) == "superseded"
    finalize_local_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_id=old["job_id"],
        final_status="completed",
        process_final_status="completed",
    )
    latest = _claim(db_path)
    assert _reserve(db_path, latest, NOW) == "start"


def test_another_users_start_does_not_delay_or_supersede_this_user(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    _queue(db_path, "first-user")
    first = _claim(db_path)
    _queue(db_path, "second-user", "user-2")
    second = _claim(db_path, "user-2")
    assert _reserve(db_path, second, NOW) == "start"
    assert _reserve(db_path, first, NOW) == "start"


def test_unchanged_insight_is_a_valid_periodic_review_input(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_insights(insight_id,user_id,status,
               short_term_insight_data,facts,prompt_name,prompt_version,created_at,updated_at)
               VALUES ('insight-1','user-1','success','Still working on the proposal.',
                       '','insight','2.2',?,?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
    source = read_reconsidered_insight(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        insight_id="insight-1",
    )
    assert source.short_term_insight == "Still working on the proposal."
    assert "Periodic review" in source.reconsideration_reason


def test_the_welcome_neither_delays_nor_supersedes_the_first_real_suggestion(
    tmp_path: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime.welcome_suggestion import (
        record_welcome_suggestion,
    )

    db_path = _db(tmp_path)
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        assert record_welcome_suggestion(
            connection=conn,
            user_id="user-1",
            answer="Welcome",
            now=NOW.isoformat().replace("+00:00", "Z"),
        )
    _queue(db_path, "first-insight", at=NOW + timedelta(minutes=5))
    first = _claim(db_path)
    # Five minutes after the welcome, the first real suggestion starts at once.
    assert _reserve(db_path, first, NOW + timedelta(minutes=5)) == "start"

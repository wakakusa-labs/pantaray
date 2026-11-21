from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.storage.migrations.connection import (
    configure_connection,
)

from .support import (
    _insert_user,
    _migrations_before,
    _migrations_through,
    apply_migrations,
)

MIGRATION_NAME = "0101_legacy_memory_agent_cutover.sql"
BUSY_TIMEOUT_MS = 1_000
CUTOVER_CODE = "LEGACY_MEMORY_AGENT_CUTOVER"
USER_ID = "user-1"
NOW = "2026-09-07T00:00:00Z"


def _seed_retired_runtime(connection: sqlite3.Connection) -> None:
    _insert_user(connection, USER_ID)
    connection.execute(
        """
        INSERT INTO processes(
            process_id, user_id, kind, status, started_at, updated_at,
            heartbeat_at, current_job_id, next_event_seq
        ) VALUES ('process-fact', ?, 'fact', 'running', ?, ?, ?, 'job-fact', 1)
        """,
        (USER_ID, NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO processes(
            process_id, user_id, kind, status, started_at, updated_at,
            heartbeat_at, current_job_id, next_event_seq
        ) VALUES ('process-short-insight', ?, 'insight', 'running', ?, ?, ?,
                  'job-short-insight', 1)
        """,
        (USER_ID, NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, user_id, job_type, process_id, status, scheduled_at,
            logical_key, claimed_by, claimed_at, heartbeat_at
        ) VALUES ('job-fact', ?, 'structure_facts', 'process-fact', 'running', ?,
                  ?, 'worker-1', ?, ?)
        """,
        (USER_ID, NOW, USER_ID, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, user_id, job_type, process_id, status, scheduled_at,
            logical_key
        ) VALUES ('job-short-insight', ?, 'generate_insight',
                  'process-short-insight', 'queued', ?, ?)
        """,
        (USER_ID, NOW, USER_ID),
    )
    connection.execute(
        """
        INSERT INTO job_attempts(
            attempt_id, job_id, attempt_number, status, started_at
        ) VALUES ('attempt-1', 'job-fact', 1, 'running', ?)
        """,
        (NOW,),
    )
    connection.execute(
        """
        INSERT INTO memory_agent_triggers(
            user_id, trigger_kind, source_id, status, dispatched_job_id,
            outcome_code, created_at, handled_at
        ) VALUES
            (?, 'fact_from_24h_summary', 'summary-1', 'dispatched', 'job-fact',
             'JOB_ENQUEUED', ?, ?),
            (?, 'insight_update_from_insight', 'insight-1', 'pending', NULL,
             NULL, ?, NULL),
            (?, 'memory_from_short_insight', 'insight-2', 'pending', NULL,
             NULL, ?, NULL)
        """,
        (USER_ID, NOW, NOW, USER_ID, NOW, USER_ID, NOW),
    )


def _apply_cutover(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.sqlite3"
    apply_migrations(
        db_path,
        BUSY_TIMEOUT_MS,
        _migrations_before(load_default_migrations(), MIGRATION_NAME),
    )
    with closing(sqlite3.connect(db_path)) as connection:
        configure_connection(connection, BUSY_TIMEOUT_MS)
        _seed_retired_runtime(connection)
        connection.commit()

    apply_migrations(
        db_path,
        BUSY_TIMEOUT_MS,
        _migrations_through(load_default_migrations(), MIGRATION_NAME),
    )
    return db_path


def test_cutover_skips_pending_retired_triggers_and_keeps_the_dispatched_history(
    tmp_path: Path,
) -> None:
    db_path = _apply_cutover(tmp_path)

    with closing(sqlite3.connect(db_path)) as connection:
        triggers = connection.execute(
            """
            SELECT trigger_kind, source_id, status, dispatched_job_id, outcome_code
            FROM memory_agent_triggers ORDER BY source_id
            """
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert triggers == [
        ("insight_update_from_insight", "insight-1", "skipped", None, CUTOVER_CODE),
        ("memory_from_short_insight", "insight-2", "pending", None, None),
        (
            "fact_from_24h_summary",
            "summary-1",
            "dispatched",
            "job-fact",
            "JOB_ENQUEUED",
        ),
    ]


def test_cutover_cancels_the_retired_job_without_touching_the_short_insight_run(
    tmp_path: Path,
) -> None:
    db_path = _apply_cutover(tmp_path)

    with closing(sqlite3.connect(db_path)) as connection:
        retired_job = connection.execute(
            """
            SELECT status, error_code, claimed_by, claimed_at, heartbeat_at
            FROM jobs WHERE job_id = 'job-fact'
            """
        ).fetchone()
        retired_process = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            ("process-fact",),
        ).fetchone()
        attempt = connection.execute(
            "SELECT status, error_code FROM job_attempts WHERE attempt_id = 'attempt-1'"
        ).fetchone()
        short_insight_job = connection.execute(
            "SELECT status FROM jobs WHERE job_id = 'job-short-insight'"
        ).fetchone()
        short_insight_process = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = ?",
            ("process-short-insight",),
        ).fetchone()

    assert retired_job == ("canceled", CUTOVER_CODE, None, None, None)
    assert retired_process == ("canceled", None)
    assert attempt == ("canceled", CUTOVER_CODE)
    assert short_insight_job == ("queued",)
    assert short_insight_process == ("running", "job-short-insight")

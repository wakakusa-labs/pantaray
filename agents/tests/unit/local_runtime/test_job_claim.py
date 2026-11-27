from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.hooks import inbox
from pantaray_agents.local_runtime.runtime.job_claim import (
    claim_next_pending_job,
    finalize_local_job,
    has_pending_job,
    peek_next_pending_job_type,
)
from pantaray_agents.local_runtime.runtime.job_envelope import (
    LocalJobEnvelopeIntegrityError,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)

from .migrated_db import prepare_test_database


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-1', 'ja', '2026-03-22T00:00:00Z', '2026-03-22T00:00:00Z')
                """
            )
    return db_path


def _insert_job(
    *,
    db_path: Path,
    job_id: str,
    process_id: str,
    scheduled_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO processes(
                    process_id,
                    user_id,
                    kind,
                    status,
                    started_at,
                    updated_at,
                    heartbeat_at,
                    next_event_seq
                ) VALUES (?, 'user-1', 'action', 'enqueued', ?, ?, ?, 1)
                """,
                (process_id, scheduled_at, scheduled_at, scheduled_at),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id,
                    user_id,
                    job_type,
                    process_id,
                    status,
                    scheduled_at,
                    logical_key
                ) VALUES (?, 'user-1', 'execute_action', ?, 'queued', ?, ?)
                """,
                (job_id, process_id, scheduled_at, f"logical-{job_id}"),
            )
            connection.execute(
                """
                INSERT INTO job_payloads(job_id, payload_json)
                VALUES (?, ?)
                """,
                (
                    job_id,
                    json.dumps(
                        {
                            "job_id": job_id,
                            "process_id": process_id,
                            "dispatch_kind": "start",
                            "action_id": f"action-{job_id}",
                            "suggestion_id": f"suggestion-{job_id}",
                            "user_id": "user-1",
                            "command_id": "command-1",
                            "accepted_at": "2026-03-22T00:00:00Z",
                            "enqueued_at": scheduled_at,
                            "screen_captures": [],
                        }
                    ),
                ),
            )


def test_owner_pending_poll_queries_use_owner_queue_index(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        peek_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT job_type FROM jobs
            WHERE user_id = ? AND status = ?
              AND scheduled_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
              AND job_type IN ('execute_action', 'activity_description')
            ORDER BY scheduled_at ASC LIMIT 1
            """,
            ("user-1", "queued"),
        ).fetchall()
        claim_plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT job_id FROM jobs
            WHERE job_type = 'execute_action' AND user_id = ?
              AND status = ?
              AND scheduled_at <= strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ORDER BY scheduled_at ASC LIMIT 1
            """,
            ("user-1", "queued"),
        ).fetchall()

    index_name = "idx_jobs_user_queued_schedule_type"
    assert any(index_name in str(row[3]) for row in peek_plan)
    assert any(index_name in str(row[3]) for row in claim_plan)


def test_future_scheduled_jobs_are_not_claimable(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    _insert_job(
        db_path=db_path,
        job_id="job-future",
        process_id="process-future",
        scheduled_at="2099-03-22T00:00:00Z",
    )

    assert (
        has_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type="execute_action",
        )
        is False
    )
    assert (
        peek_next_pending_job_type(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_types=("execute_action",),
            owner_user_id="user-1",
        )
        is None
    )
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type="execute_action",
            owner_user_id="user-1",
            claimed_by="worker-1",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is None
    )


@pytest.mark.parametrize(
    ("process_update", "expected_process"),
    [
        (
            "user_id = 'user-2'",
            ("user-2", "action", "enqueued", None),
        ),
        (
            "kind = 'fact'",
            ("user-1", "fact", "enqueued", None),
        ),
        (
            "status = 'running'",
            ("user-1", "action", "running", None),
        ),
    ],
)
def test_claim_blocks_invalid_job_envelope_without_mutating_process(
    tmp_path: Path,
    process_update: str,
    expected_process: tuple[str, str, str, str | None],
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _insert_job(
        db_path=db_path,
        job_id="job-invalid",
        process_id="process-invalid",
        scheduled_at="2026-03-22T00:00:00Z",
    )
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-2', 'ja', '2026-03-22T00:00:00Z',
                        '2026-03-22T00:00:00Z')
                """
            )
            connection.execute(
                f"UPDATE processes SET {process_update} WHERE process_id = ?",
                ("process-invalid",),
            )

    with pytest.raises(LocalJobEnvelopeIntegrityError):
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type="execute_action",
            owner_user_id="user-1",
            claimed_by="worker-1",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )

    with sqlite3.connect(db_path) as connection:
        job = connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id = 'job-invalid'"
        ).fetchone()
        process = connection.execute(
            """
            SELECT user_id, kind, status, current_job_id
            FROM processes WHERE process_id = 'process-invalid'
            """
        ).fetchone()
        attempt_count = connection.execute(
            "SELECT COUNT(*) FROM job_attempts WHERE job_id = 'job-invalid'"
        ).fetchone()

    assert job == ("blocked", "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR")
    assert process == expected_process
    assert attempt_count == (0,)


def test_finalize_blocks_job_if_process_owner_changes_after_claim(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    _insert_job(
        db_path=db_path,
        job_id="job-owner-changed",
        process_id="process-owner-changed",
        scheduled_at="2026-03-22T00:00:00Z",
    )
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO users(user_id, ui_language, created_at, updated_at)
                VALUES ('user-2', 'ja', '2026-03-22T00:00:00Z',
                        '2026-03-22T00:00:00Z')
                """
            )
            connection.execute(
                "UPDATE processes SET user_id = 'user-2' WHERE process_id = ?",
                ("process-owner-changed",),
            )

    with pytest.raises(LocalJobEnvelopeIntegrityError):
        finalize_local_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_id="job-owner-changed",
            final_status="completed",
            process_final_status="completed",
        )

    with sqlite3.connect(db_path) as connection:
        job = connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id = 'job-owner-changed'"
        ).fetchone()
        process = connection.execute(
            """
            SELECT user_id, kind, status, current_job_id
            FROM processes WHERE process_id = 'process-owner-changed'
            """
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT status, error_code FROM job_attempts
            WHERE job_id = 'job-owner-changed'
            """
        ).fetchone()

    assert job == ("blocked", "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR")
    assert process == (
        "user-2",
        "action",
        "running",
        "job-owner-changed",
    )
    assert attempt == ("failed", "LOCAL_JOB_ENVELOPE_INTEGRITY_ERROR")


def test_finalize_without_a_failure_message_records_no_reason(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    _insert_job(
        db_path=db_path,
        job_id="job-completed",
        process_id="process-completed",
        scheduled_at="2026-03-22T00:00:00Z",
    )
    assert (
        claim_next_pending_job(
            db_path=str(db_path),
            busy_timeout_ms=1_000,
            job_type="execute_action",
            owner_user_id="user-1",
            claimed_by="worker-1",
            process_running_status="running",
            expected_process_pending_status="enqueued",
        )
        is not None
    )

    finalize_local_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_id="job-completed",
        final_status="completed",
        process_final_status="completed",
    )

    with sqlite3.connect(db_path) as connection:
        job = connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id = 'job-completed'"
        ).fetchone()
        attempt = connection.execute(
            """
            SELECT status, error_code, error_message FROM job_attempts
            WHERE job_id = 'job-completed'
            """
        ).fetchone()

    assert job == ("completed", None)
    assert attempt == ("completed", None, None)


def test_a_job_writes_its_lifecycle_to_the_inboxes_that_listen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a lifecycle event missing for a hook that listens to it, and an
    # abandoned job that no failure hook hears of.
    listens = ("execute_action.started", "execute_action.failed")
    monkeypatch.setattr(
        inbox,
        "_listening_hooks",
        lambda event: ("h",) if event.name in listens else (),
    )
    db_path = _bootstrap_db(tmp_path)
    _insert_job(
        db_path=db_path,
        job_id="job-1",
        process_id="process-1",
        scheduled_at="2026-03-22T00:00:00Z",
    )
    assert claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type="execute_action",
        owner_user_id="user-1",
        claimed_by="worker-1",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    finalize_local_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_id="job-1",
        final_status="abandoned",
        process_final_status="failed",
    )

    pending = inbox.SqliteHookInbox(
        db_path=db_path, busy_timeout_ms=1_000, user_id="user-1"
    ).read("h")
    assert tuple(event.name for event in pending) == listens

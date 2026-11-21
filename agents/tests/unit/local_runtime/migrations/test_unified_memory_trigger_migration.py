from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

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

MIGRATION_NAME = "0099_unified_memory_triggers.sql"
USER_ID = "user-1"
NOW = "2026-09-07T00:00:00Z"


def _apply_before(path: Path) -> None:
    apply_migrations(
        path, 1000, _migrations_before(load_default_migrations(), MIGRATION_NAME)
    )


def _apply_through_this_migration(path: Path) -> None:
    """A later cutover rewrites retired trigger rows, so this stops at v99."""

    apply_migrations(
        path, 1000, _migrations_through(load_default_migrations(), MIGRATION_NAME)
    )


def _seed_dispatched_and_pending_triggers(connection: sqlite3.Connection) -> None:
    _insert_user(connection, USER_ID)
    connection.execute(
        """
        INSERT INTO processes(
            process_id, user_id, kind, status, started_at, updated_at,
            heartbeat_at, next_event_seq
        ) VALUES ('process-fact', ?, 'fact', 'enqueued', ?, ?, ?, 1)
        """,
        (USER_ID, NOW, NOW, NOW),
    )
    connection.execute(
        """
        INSERT INTO jobs(
            job_id, user_id, job_type, process_id, status, scheduled_at,
            logical_key
        ) VALUES ('job-fact', ?, 'structure_facts', 'process-fact', 'queued', ?, ?)
        """,
        (USER_ID, NOW, USER_ID),
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
             NULL, ?, NULL)
        """,
        (USER_ID, NOW, NOW, USER_ID, NOW),
    )


def test_upgrade_keeps_existing_trigger_rows_and_their_job_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    _apply_before(path)
    with closing(sqlite3.connect(path)) as connection:
        configure_connection(connection, 1000)
        _seed_dispatched_and_pending_triggers(connection)
        connection.commit()

    _apply_through_this_migration(path)

    with closing(sqlite3.connect(path)) as connection:
        configure_connection(connection, 1000)
        assert connection.execute(
            """
            SELECT trigger_kind, source_id, status, dispatched_job_id, outcome_code
            FROM memory_agent_triggers ORDER BY source_id
            """
        ).fetchall() == [
            ("insight_update_from_insight", "insight-1", "pending", None, None),
            (
                "fact_from_24h_summary",
                "summary-1",
                "dispatched",
                "job-fact",
                "JOB_ENQUEUED",
            ),
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        # The recreated jobs foreign key still rejects an unknown job binding.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE memory_agent_triggers SET dispatched_job_id = 'job-missing'
                WHERE source_id = 'summary-1'
                """
            )


def test_upgrade_admits_the_memory_process_kind(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite3"
    apply_migrations(path, 1000, load_default_migrations())

    with closing(sqlite3.connect(path)) as connection:
        configure_connection(connection, 1000)
        _insert_user(connection, USER_ID)
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, started_at, updated_at,
                heartbeat_at, next_event_seq
            ) VALUES ('process-memory', ?, 'memory', 'enqueued', ?, ?, ?, 1)
            """,
            (USER_ID, NOW, NOW, NOW),
        )
        connection.commit()
        assert connection.execute(
            "SELECT kind FROM processes WHERE process_id = 'process-memory'"
        ).fetchone() == ("memory",)

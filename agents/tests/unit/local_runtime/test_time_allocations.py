from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.time_allocations import (
    render_time_by_endeavor,
)
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .migrated_db import prepare_test_database

USER = "user-1"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "runtime.db"
    prepare_test_database(path, 1_000, load_default_migrations())
    with open_memory_catalog_connection(db_path=path, busy_timeout_ms=1_000) as conn:
        for user in (USER, "other"):
            conn.execute(
                "INSERT INTO users VALUES (?, 'ja', NULL, 'now', 'now')", (user,)
            )
        conn.commit()
    return sqlite3.connect(path)


def _allocate(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    observed_start: str,
    endeavor: str,
    minutes: int,
    organization: str | None = None,
    user_id: str = USER,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO activity_logs(log_id, user_id, period_start, period_end, "
        "description, status, prompt_name, prompt_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'a', 'success', 'insight', '2.6', 'now', 'now')",
        (run_id, user_id, observed_start, observed_start),
    )
    connection.execute(
        "INSERT INTO activity_time_allocations(allocation_id, user_id, run_id, "
        "observed_start, observed_end, endeavor, organization_name, project_name, "
        "minutes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 'now')",
        (
            f"{run_id}-{endeavor}",
            user_id,
            run_id,
            observed_start,
            observed_start,
            endeavor,
            organization,
            minutes,
        ),
    )


def test_each_window_lists_where_the_time_went_largest_first(
    connection: sqlite3.Connection,
) -> None:
    _allocate(
        connection,
        run_id="this-week",
        observed_start="2026-10-26T01:00:00Z",
        endeavor="見積",
        minutes=90,
        organization="A社",
    )
    _allocate(
        connection,
        run_id="this-week",
        observed_start="2026-10-26T01:00:00Z",
        endeavor="Pantaray",
        minutes=30,
    )
    _allocate(
        connection,
        run_id="last-month",
        observed_start="2026-10-05T01:00:00Z",
        endeavor="論文",
        minutes=120,
    )
    _allocate(
        connection,
        run_id="too-old",
        observed_start="2026-09-01T01:00:00Z",
        endeavor="昔の案件",
        minutes=600,
    )
    _allocate(
        connection,
        run_id="someone-else",
        observed_start="2026-10-26T01:00:00Z",
        endeavor="他人の仕事",
        minutes=600,
        user_id="other",
    )

    rendered = render_time_by_endeavor(connection, user_id=USER, now=NOW)

    assert rendered == (
        "Last 7 days (since 2026-10-20T12:00:00Z, 2.0h observed):\n"
        "- 見積 (A社): 1.5h (75%)\n"
        "- Pantaray: 0.5h (25%)\n"
        "\n"
        "Last 28 days (since 2026-09-29T12:00:00Z, 4.0h observed):\n"
        "- 論文: 2.0h (50%)\n"
        "- 見積 (A社): 1.5h (38%)\n"
        "- Pantaray: 0.5h (12%)"
    )


def test_nothing_is_rendered_before_any_time_is_recorded(
    connection: sqlite3.Connection,
) -> None:
    assert render_time_by_endeavor(connection, user_id=USER, now=NOW) == ""

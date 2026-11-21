from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.local_runtime.hooks.runner import SqliteHookStateStore
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .support import _migrations_before, apply_migrations


def test_summary_hooks_continue_from_the_scheduler_next_run_times(
    tmp_path: Path,
) -> None:
    # Catches an upgrade that refires every summary at once (state not seeded or
    # seeded under the wrong hook id), loses the summary cursor, or leaves the
    # retired column to drift.
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(migrations, "0147_hook_states.sql"),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES ('user-1', 'ja', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
        )
        connection.executemany(
            """INSERT INTO scheduler_jobs(job_name, job_kind, status, cursor_value,
                   next_run_after, consecutive_failures)
               VALUES (?, 'activity_summary', 'active', ?, ?, 0)""",
            [
                ("activity_summary_1h", "2026-11-21T08:00:00Z", "2026-11-21T10:05:00Z"),
                ("activity_summary_3m", None, None),
            ],
        )

    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=migrations)

    states = SqliteHookStateStore(
        db_path=db_path, busy_timeout_ms=1_000, user_id="user-1"
    )
    assert states.read_next_fire_times() == {
        "builtin.summary_1h": datetime(2026, 11, 21, 10, 5, tzinfo=UTC)
    }
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(
            "SELECT job_name, cursor_value FROM scheduler_jobs ORDER BY job_name"
        ).fetchall() == [
            ("activity_summary_1h", "2026-11-21T08:00:00Z"),
            ("activity_summary_3m", None),
        ]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(scheduler_jobs)")
        }
    assert "next_run_after" not in columns


def test_every_owner_continues_from_the_shared_state_across_the_upgrade(
    tmp_path: Path,
) -> None:
    # Catches an upgrade that drops the shared state (every time hook refires
    # at once for everyone) or keeps it for one owner only (the others refire).
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(migrations, "0150_hook_states_per_user.sql"),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executemany(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'ja', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')",
            [("user-1",), ("user-2",)],
        )
        connection.executemany(
            "INSERT INTO hook_states(hook_id, next_fire_at) VALUES (?, ?)",
            [
                ("builtin.insight", "2026-11-21T10:15:00Z"),
                ("builtin.summary_1h", "2026-11-21T11:05:00Z"),
            ],
        )

    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=migrations)

    for user_id in ("user-1", "user-2"):
        states = SqliteHookStateStore(
            db_path=db_path, busy_timeout_ms=1_000, user_id=user_id
        )
        assert states.read_next_fire_times() == {
            "builtin.insight": datetime(2026, 11, 21, 10, 15, tzinfo=UTC),
            "builtin.summary_1h": datetime(2026, 11, 21, 11, 5, tzinfo=UTC),
        }

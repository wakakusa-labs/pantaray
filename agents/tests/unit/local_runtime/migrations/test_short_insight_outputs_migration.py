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

MIGRATION_NAME = "0098_short_insight_outputs.sql"


def test_upgrade_adds_the_reason_column_and_retires_hourly_insight_triggers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    migrations = load_default_migrations()
    apply_migrations(path, 1000, _migrations_before(migrations, MIGRATION_NAME))
    with closing(sqlite3.connect(path)) as connection:
        configure_connection(connection, 1000)
        _insert_user(connection, "user-1")
        connection.execute(
            """
            INSERT INTO memory_agent_triggers(
                user_id, trigger_kind, source_id, status, created_at
            ) VALUES
                ('user-1', 'insight_from_1h_summary', 'summary-1', 'pending', 'now'),
                ('user-1', 'fact_from_24h_summary', 'summary-2', 'pending', 'now')
            """
        )
        connection.commit()

    apply_migrations(path, 1000, _migrations_through(migrations, MIGRATION_NAME))

    with closing(sqlite3.connect(path)) as connection:
        configure_connection(connection, 1000)
        connection.execute(
            """
            INSERT INTO agent_insights(
                insight_id, user_id, status, short_term_insight_data, facts,
                reconsideration_reason, prompt_name, prompt_version,
                created_at, updated_at
            ) VALUES (
                'insight-1', 'user-1', 'success', '# Insight', '',
                'The user changed goals.', 'insight', '2.0', 'now', 'now'
            )
            """
        )
        connection.commit()
        assert connection.execute(
            "SELECT reconsideration_reason FROM agent_insights WHERE insight_id = ?",
            ("insight-1",),
        ).fetchone() == ("The user changed goals.",)
        assert connection.execute(
            "SELECT trigger_kind FROM memory_agent_triggers"
        ).fetchall() == [("fact_from_24h_summary",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

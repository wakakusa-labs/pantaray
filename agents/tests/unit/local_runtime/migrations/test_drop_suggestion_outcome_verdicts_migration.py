from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations as load_all_migrations,
)

from .support import _insert_user, _migrations_through, apply_migrations

VERDICTS_MIGRATION_NAME = "0133_suggestion_outcome_verdicts.sql"
DROP_MIGRATION_NAME = "0134_drop_suggestion_outcome_verdicts.sql"
BUSY_TIMEOUT_MS = 1_000


def test_a_store_holding_verdicts_upgrades_without_them(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_all_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, VERDICTS_MIGRATION_NAME),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, answer, has_suggestion,
                interaction_contract, created_at, updated_at
            ) VALUES ('s-1', 'user-1', 'success', 'Offer.', 1, 'message_only',
                      '2026-10-28T00:00:00Z', '2026-10-28T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO suggestion_outcome_verdicts(
                verdict_id, user_id, suggestion_id, checkpoint_days, verdict,
                evidence_json, reason, judged_at, job_id, prompt_name,
                prompt_version
            ) VALUES ('v-1', 'user-1', 's-1', 1, 'unknown', '[]', 'Unknown.',
                      '2026-10-29T00:00:00Z', 'job-1', 'memory_update_outcomes',
                      '1.0')
            """
        )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=_migrations_through(migrations, DROP_MIGRATION_NAME),
    )

    with closing(sqlite3.connect(db_path)) as connection:
        remaining = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name LIKE '%outcome_verdict%'"
        ).fetchall()
        suggestions = connection.execute(
            "SELECT suggestion_id FROM agent_suggestions"
        ).fetchall()
    assert remaining == []
    assert suggestions == [("s-1",)]

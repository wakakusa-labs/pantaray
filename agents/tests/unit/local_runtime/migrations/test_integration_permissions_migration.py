"""Widening approval_mode must keep every saved preference and its grants.

These use the production migration list rather than `support.load_default_migrations`,
which stops below the reset version and would never reach this migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    verify_database_integrity,
)
from pantaray_agents.local_runtime.storage.migrations.specs import (
    load_default_migrations,
)

BUSY_TIMEOUT_MS = 1_000
MIGRATION_VERSION = 130
TIMESTAMP = "2026-10-18T00:00:00Z"


def _insert_preference(
    connection: sqlite3.Connection,
    *,
    preference_id: str,
    approval_mode: str,
    applies_to_json: str,
) -> None:
    connection.execute(
        "INSERT INTO approval_preferences(preference_id, user_id, scope_type,"
        " scope_ref, approval_mode, applies_to_json, created_at, updated_at,"
        " updated_by, revoked_at) VALUES (?, 'user-1', 'global', NULL, ?, ?, ?, ?,"
        " 'user', NULL)",
        (preference_id, approval_mode, applies_to_json, TIMESTAMP, TIMESTAMP),
    )


def test_saved_preferences_and_grants_survive_and_new_modes_are_constrained(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=tuple(m for m in migrations if m.version < MIGRATION_VERSION),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES ('user-1', 'ja', ?, ?)",
            (TIMESTAMP, TIMESTAMP),
        )
        _insert_preference(
            connection,
            preference_id="pref-workspace",
            approval_mode="always_allow",
            applies_to_json='["workspace_edit_and_command"]',
        )
        connection.execute(
            "INSERT INTO capability_grants(grant_id, user_id, preference_id,"
            " capability, scope_type, scope_ref, grant_source, granted_at,"
            " granted_by) VALUES ('grant-1', 'user-1', 'pref-workspace',"
            " 'process_exec_local', 'global', NULL, 'settings', ?, 'user')",
            (TIMESTAMP,),
        )

    apply_migrations(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS, migrations=migrations
    )
    verify_database_integrity(db_path, BUSY_TIMEOUT_MS)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT preference_id, approval_mode, applies_to_json"
            " FROM approval_preferences"
        ).fetchall() == [
            ("pref-workspace", "always_allow", '["workspace_edit_and_command"]')
        ]
        # The grant still points at its preference, and deleting the preference
        # still detaches it: the foreign key follows the rebuilt table.
        connection.execute(
            "DELETE FROM approval_preferences WHERE preference_id = 'pref-workspace'"
        )
        assert connection.execute(
            "SELECT preference_id FROM capability_grants WHERE grant_id = 'grant-1'"
        ).fetchone() == (None,)

        _insert_preference(
            connection,
            preference_id="pref-read",
            approval_mode="never_allow",
            applies_to_json='["integration_read:gmail:account-a"]',
        )
        _insert_preference(
            connection,
            preference_id="pref-write",
            approval_mode="prompt_each_time",
            applies_to_json='["integration_write:gmail:account-a"]',
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "UPDATE approval_preferences SET approval_mode = 'always_allow'"
                " WHERE preference_id = 'pref-write'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_preference(
                connection,
                preference_id="pref-invalid",
                approval_mode="sometimes",
                applies_to_json='["screen_capture"]',
            )

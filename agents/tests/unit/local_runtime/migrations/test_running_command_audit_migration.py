from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .support import _migrations_before, apply_migrations


def _insert_command_audit(
    connection: sqlite3.Connection,
    invocation_id: str,
    *,
    outcome: str,
    exit_code: int | None,
) -> None:
    connection.execute(
        """
        INSERT INTO command_invocation_audits(
            invocation_id, execution_kind, executable_source_kind,
            resolved_executable_path, terminal_outcome, exit_code, stdout_bytes,
            stdout_max_bytes, stderr_max_bytes, temp_storage_limit_bytes,
            child_count_limit, open_file_lease_limit, created_at, updated_at
        ) VALUES (?, 'workspace_command', 'trusted_system_executable',
            '/bin/zsh', ?, ?, 6, 100, 100, 100, 8, 8, 't0', 't1')
        """,
        (invocation_id, outcome, exit_code),
    )


def test_running_command_audit_migration_keeps_rows_and_accepts_running(
    tmp_path: Path,
) -> None:
    # Catches a rebuild that drops or alters stored audits or loses an index,
    # and a table that rejects a running row or accepts one with an exit code.
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations, "0146_running_command_invocation_audits.sql"
        ),
    )
    # Foreign keys stay off: the audits' parent invocations are not under test.
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_command_audit(connection, "ended", outcome="exited", exit_code=0)

    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=migrations)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT invocation_id, terminal_outcome, exit_code, stdout_bytes, "
            "created_at, updated_at FROM command_invocation_audits"
        ).fetchall() == [("ended", "exited", 0, 6, "t0", "t1")]
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'command_invocation_audits' AND sql IS NOT NULL"
            )
        } == {
            "idx_command_invocation_audits_outcome_created",
            "idx_command_invocation_audits_exec_kind_created",
        }
        _insert_command_audit(connection, "running", outcome="running", exit_code=None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_command_audit(connection, "coded", outcome="running", exit_code=-1)

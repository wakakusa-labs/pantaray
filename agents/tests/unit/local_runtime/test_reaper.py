from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.reaper import run_local_periodic_reaper_once
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)

from .migrated_db import prepare_test_database


def test_run_local_periodic_reaper_once_records_runtime_recovery_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.reaper.reconcile_tool_runtime_resources_for_periodic_reaper",
        lambda **_: 3,
    )

    recovered_count = run_local_periodic_reaper_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    assert recovered_count == 3
    with sqlite3.connect(db_path) as connection:
        run_row = connection.execute(
            """
            SELECT status, note
            FROM runtime_recovery_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert run_row == (
        "completed",
        "periodic deterministic recovery checkpoint: recovered_tool_resources=3",
    )

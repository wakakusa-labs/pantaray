from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.activity_log_seed import seed_activity_log

from pantaray_agents.local_runtime.runtime.activity_source_rows import (
    persist_activity_summary_result,
    reserve_activity_summary_processing,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)

from .migrated_db import prepare_test_database


@pytest.fixture(autouse=True)
def _reset_session_store() -> None:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()


def _insert_user(db_path: Path, *, user_id: str = "user-1") -> None:
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=user_id,
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version="1",
    )


def _persist_successful_log(
    db_path: Path,
    *,
    log_id: str = "log-1",
    period_start: str = "2026-03-27T00:00:00Z",
    period_end: str = "2026-03-27T00:04:00Z",
) -> None:
    seed_activity_log(
        db_path,
        log_id=log_id,
        user_id="user-1",
        period_start=period_start,
        period_end=period_end,
    )


def test_persist_activity_summary_result_requires_existing_source_row(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user(db_path)

    try:
        persist_activity_summary_result(
            db_path=db_path,
            busy_timeout_ms=1_000,
            summary_id="missing-summary",
            user_id="user-1",
            summary_type="24h",
            period_start="2026-03-26T00:00:00Z",
            period_end="2026-03-27T00:00:00Z",
            summary="summary",
            status="success",
            error=None,
            prompt_text="prompt",
            thinking=None,
            source_ids=[],
            updated_at="2026-03-27T00:04:10Z",
        )
    except MigrationError as exc:
        assert "activity_summary result row not found" in str(exc)
    else:
        raise AssertionError("persist_activity_summary_result must fail-closed")


def test_persist_activity_summary_result_writes_thinking(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user(db_path)
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id="summary-1",
        user_id="user-1",
        summary_type="24h",
        period_start="2026-03-26T00:00:00Z",
        period_end="2026-03-27T00:00:00Z",
        created_at="2026-03-27T00:00:10Z",
    )

    persist_activity_summary_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id="summary-1",
        user_id="user-1",
        summary_type="24h",
        period_start="2026-03-26T00:00:00Z",
        period_end="2026-03-27T00:00:00Z",
        summary="summary",
        status="success",
        error=None,
        prompt_text="prompt",
        thinking="summary thought",
        source_ids=[],
        updated_at="2026-03-27T00:04:10Z",
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT thinking FROM activity_summaries WHERE summary_id = ?",
            ("summary-1",),
        ).fetchone()

    assert row == ("summary thought",)


def _reserved_daily_summary(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    _persist_successful_log(db_path)
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id="summary-24h",
        user_id="user-1",
        summary_type="24h",
        period_start="2026-03-26T00:00:00Z",
        period_end="2026-03-27T00:00:00Z",
        created_at="2026-03-27T01:00:10Z",
    )
    return db_path, {
        "db_path": db_path,
        "busy_timeout_ms": 1_000,
        "summary_id": "summary-24h",
        "user_id": "user-1",
        "summary_type": "24h",
        "period_start": "2026-03-26T00:00:00Z",
        "period_end": "2026-03-27T00:00:00Z",
        "summary": "summary",
        "status": "success",
        "error": None,
        "prompt_text": "prompt",
        "thinking": None,
        "source_ids": ["log-1"],
        "updated_at": "2026-03-27T01:00:20Z",
    }


def test_successful_daily_summary_records_one_event(
    tmp_path: Path,
) -> None:
    # Catches a replayed terminal write recording the summary twice, which
    # would hand it to two Memory runs.
    db_path, result = _reserved_daily_summary(tmp_path)

    persist_activity_summary_result(**result)
    persist_activity_summary_result(**result)

    with sqlite3.connect(db_path) as connection:
        events = connection.execute(
            "SELECT event_name, payload_json FROM hook_inbox"
        ).fetchall()
    assert events == [
        (
            "activity_summary.recorded",
            '{"summary_id": "summary-24h", "summary_type": "24h"}',
        )
    ]


def test_activity_summary_terminal_result_cannot_be_overwritten(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    _insert_user(db_path)
    _persist_successful_log(db_path)
    reserve_activity_summary_processing(
        db_path=db_path,
        busy_timeout_ms=1_000,
        summary_id="summary-1",
        user_id="user-1",
        summary_type="1h",
        period_start="2026-03-27T00:00:00Z",
        period_end="2026-03-27T01:00:00Z",
        created_at="2026-03-27T01:00:10Z",
    )
    result = {
        "db_path": db_path,
        "busy_timeout_ms": 1_000,
        "summary_id": "summary-1",
        "user_id": "user-1",
        "summary_type": "1h",
        "period_start": "2026-03-27T00:00:00Z",
        "period_end": "2026-03-27T01:00:00Z",
        "summary": "original",
        "status": "success",
        "error": None,
        "prompt_text": "prompt",
        "thinking": None,
        "source_ids": ["log-1"],
        "updated_at": "2026-03-27T01:00:20Z",
    }
    persist_activity_summary_result(**result)

    with pytest.raises(MigrationError, match="cannot be overwritten"):
        persist_activity_summary_result(**{**result, "summary": "replacement"})

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT summary FROM activity_summaries WHERE summary_id = 'summary-1'"
        ).fetchone()
    assert row == ("original",)

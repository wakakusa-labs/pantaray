from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.activity_summary_schedule import SummaryType, iso_z
from pantaray_agents.local_runtime.runtime.activity_queue import (
    enqueue_local_activity_summary_job,
)
from pantaray_agents.local_runtime.runtime.activity_summary_recovery import (
    recover_interrupted_activity_summary_scheduler_runs,
)
from pantaray_agents.local_runtime.runtime.activity_summary_scheduler import (
    run_activity_summary_scheduler_once,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    build_local_insight_enqueue_request,
    build_short_insight_job_payload,
)
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)

from .migrated_db import prepare_test_database


def _insert_user_row(db_path: Path, *, user_id: str, updated_at: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users(user_id, ui_language, created_at, updated_at)
            VALUES (?, 'ja', ?, ?)
            """,
            (user_id, updated_at, updated_at),
        )
        connection.commit()


def _activate_session(db_path: Path, *, user_id: str = "user-1") -> None:
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=user_id,
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version="1",
    )


@pytest.fixture(autouse=True)
def _reset_owner_state() -> None:
    reset_desktop_session_store()
    yield
    reset_desktop_session_store()
    reset_logged_out_owner()


def _read_scheduler_job_cursor_values(db_path: Path) -> dict[str, str | None]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT job_name, cursor_value
            FROM scheduler_jobs
            ORDER BY job_name ASC
            """
        ).fetchall()
    return {
        str(job_name): (str(cursor_value) if cursor_value is not None else None)
        for job_name, cursor_value in rows
    }


def _seed_scheduler_cursor(db_path: Path, job_name: str, cursor_value: str) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO scheduler_jobs(
                job_name, job_kind, status, cursor_value, consecutive_failures
            ) VALUES (?, 'activity_summary', 'active', ?, 0)
            """,
            (job_name, cursor_value),
        )


def _read_scheduler_job_runs(db_path: Path, job_name: str) -> list[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT logical_window_start, status
            FROM scheduler_job_runs
            WHERE job_name = ?
            ORDER BY logical_window_start ASC
            """,
            (job_name,),
        ).fetchall()
    return [(str(window_start), str(status)) for window_start, status in rows]


def test_startup_recovery_fails_only_interrupted_activity_summary_runs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            """
            INSERT INTO scheduler_jobs(job_name, job_kind, status)
            VALUES ('activity_summary_1h', 'activity_summary', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO scheduler_job_runs(
                run_id, job_name, logical_window_start, logical_window_end,
                status, attempt, enqueued_at, started_at
            ) VALUES (
                'run-1', 'activity_summary_1h',
                '2026-08-16T00:00:00Z', '2026-08-16T01:00:00Z',
                'running', 1, '2026-08-16T01:05:00Z', '2026-08-16T01:05:00Z'
            )
            """
        )

    first = recover_interrupted_activity_summary_scheduler_runs(
        db_path=db_path,
        busy_timeout_ms=1_000,
        recovered_at=datetime(2026, 8, 16, 2, 0, tzinfo=UTC),
    )
    second = recover_interrupted_activity_summary_scheduler_runs(
        db_path=db_path,
        busy_timeout_ms=1_000,
        recovered_at=datetime(2026, 8, 16, 2, 1, tzinfo=UTC),
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT status, error_code, completed_at
            FROM scheduler_job_runs WHERE run_id = 'run-1'
            """
        ).fetchone()
    assert first == 1
    assert second == 0
    assert row == (
        "failed",
        "SCHEDULER_PROCESS_RESTARTED",
        "2026-08-16T02:00:00.000Z",
    )


@pytest.mark.asyncio
async def test_activity_summary_scheduler_enqueues_latest_window_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _activate_session(db_path)

    enqueue_calls: list[dict[str, object]] = []

    def _fake_enqueue_activity_summary_job(**kwargs: object) -> dict[str, str]:
        enqueue_calls.append(dict(kwargs))
        return {
            "job_id": f"job-{len(enqueue_calls)}",
            "process_id": f"process-{len(enqueue_calls)}",
            "summary_id": str(kwargs["summary_id"]),
        }

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        _fake_enqueue_activity_summary_job,
    )

    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)
    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=now,
    )

    assert len(enqueue_calls) == 5
    assert {str(call["summary_type"]) for call in enqueue_calls} == {
        "1h",
        "24h",
        "1w",
        "1m",
        "3m",
    }
    assert all(
        "trigger_fact_structuring_after_success" not in call for call in enqueue_calls
    )

    first_cursor_values = _read_scheduler_job_cursor_values(db_path)
    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=now,
    )
    second_cursor_values = _read_scheduler_job_cursor_values(db_path)

    assert len(enqueue_calls) == 5
    assert first_cursor_values == second_cursor_values


@pytest.mark.asyncio
async def test_activity_summary_scheduler_retries_failed_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _activate_session(db_path)

    calls: list[str] = []
    should_fail = True

    def _fake_enqueue_activity_summary_job(**kwargs: object) -> dict[str, str]:
        nonlocal should_fail
        summary_type = str(kwargs["summary_type"])
        if summary_type == "1h" and should_fail:
            should_fail = False
            raise RuntimeError("boom")
        calls.append(summary_type)
        return {
            "job_id": f"job-{len(calls)}",
            "process_id": f"process-{len(calls)}",
            "summary_id": str(kwargs["summary_id"]),
        }

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        _fake_enqueue_activity_summary_job,
    )

    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)
    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=now,
    )
    assert "1h" not in calls

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=now,
    )
    assert calls.count("1h") == 1


@pytest.mark.asyncio
async def test_activity_summary_scheduler_backfills_from_cursor_without_skipping_windows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _activate_session(db_path)

    enqueue_calls: list[dict[str, object]] = []

    def _fake_enqueue_activity_summary_job(**kwargs: object) -> dict[str, str]:
        enqueue_calls.append(dict(kwargs))
        return {
            "job_id": f"job-{len(enqueue_calls)}",
            "process_id": f"process-{len(enqueue_calls)}",
            "summary_id": str(kwargs["summary_id"]),
        }

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        _fake_enqueue_activity_summary_job,
    )

    _seed_scheduler_cursor(db_path, "activity_summary_1h", "2026-04-15T07:00:00Z")

    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=now,
    )
    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if str(call["summary_type"]) == "1h"
    ] == ["2026-04-15T08:00:00Z"]

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=now,
    )
    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if str(call["summary_type"]) == "1h"
    ] == ["2026-04-15T08:00:00Z", "2026-04-15T09:00:00Z"]

    cursor_values = _read_scheduler_job_cursor_values(db_path)
    assert cursor_values["activity_summary_1h"] == "2026-04-15T09:00:00Z"
    assert _read_scheduler_job_runs(db_path, "activity_summary_1h") == [
        ("2026-04-15T08:00:00Z", "completed"),
        ("2026-04-15T09:00:00Z", "completed"),
    ]


@pytest.mark.asyncio
async def test_activity_summary_scheduler_runs_for_the_logged_out_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Summaries are produced with no Pantaray account signed in."""
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user_row(db_path, user_id="local-owner", updated_at="2026-04-14T00:00:00Z")
    register_logged_out_owner("local-owner")

    enqueue_calls: list[dict[str, object]] = []

    def _fake_enqueue_activity_summary_job(**kwargs: object) -> dict[str, str]:
        enqueue_calls.append(dict(kwargs))
        return {
            "job_id": f"job-{len(enqueue_calls)}",
            "process_id": f"process-{len(enqueue_calls)}",
            "summary_id": str(kwargs["summary_id"]),
        }

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        _fake_enqueue_activity_summary_job,
    )

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert {str(call["user_id"]) for call in enqueue_calls} == {"local-owner"}


@pytest.mark.asyncio
async def test_activity_summary_scheduler_ignores_historical_user_rows_and_uses_active_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _insert_user_row(
        db_path,
        user_id="stale-user",
        updated_at="2026-04-14T00:00:00Z",
    )
    _activate_session(db_path, user_id="active-user")

    enqueue_calls: list[dict[str, object]] = []

    def _fake_enqueue_activity_summary_job(**kwargs: object) -> dict[str, str]:
        enqueue_calls.append(dict(kwargs))
        return {
            "job_id": f"job-{len(enqueue_calls)}",
            "process_id": f"process-{len(enqueue_calls)}",
            "summary_id": str(kwargs["summary_id"]),
        }

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        _fake_enqueue_activity_summary_job,
    )

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert enqueue_calls
    assert {str(call["user_id"]) for call in enqueue_calls} == {"active-user"}


def _enqueue_short_insight_job(db_path: Path, window_start: datetime) -> str:
    return enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_insight_enqueue_request(
            build_short_insight_job_payload(user_id="user-1", window_start=window_start)
        ),
    )["job_id"]


def _finalize_job(db_path: Path, job_id: str) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute(
            "UPDATE jobs SET status = 'failed' WHERE job_id = ?", (job_id,)
        )


def _scheduler_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, list[dict[str, object]]]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    _activate_session(db_path)
    enqueue_calls: list[dict[str, object]] = []

    def _fake_enqueue_activity_summary_job(**kwargs: object) -> dict[str, str]:
        enqueue_calls.append(dict(kwargs))
        return {
            "job_id": f"job-{len(enqueue_calls)}",
            "process_id": f"process-{len(enqueue_calls)}",
            "summary_id": str(kwargs["summary_id"]),
        }

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.activity_summary_scheduler.enqueue_activity_summary_job",
        _fake_enqueue_activity_summary_job,
    )
    return db_path, enqueue_calls


@pytest.mark.asyncio
async def test_activity_summary_scheduler_waits_for_the_hours_short_insight_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An hour summarized before its last run lands loses those rows for good."""
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    job_id = _enqueue_short_insight_job(
        db_path, datetime(2026, 4, 15, 9, 45, tzinfo=UTC)
    )
    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)

    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [call for call in enqueue_calls if call["summary_type"] == "1h"] == []
    assert _read_scheduler_job_cursor_values(db_path)["activity_summary_1h"] is None
    assert _read_scheduler_job_runs(db_path, "activity_summary_1h") == []

    _finalize_job(db_path, job_id)
    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "1h"
    ] == ["2026-04-15T09:00:00Z"]
    assert (
        _read_scheduler_job_cursor_values(db_path)["activity_summary_1h"]
        == "2026-04-15T09:00:00Z"
    )


@pytest.mark.asyncio
async def test_activity_summary_scheduler_ignores_a_run_from_another_hour(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _enqueue_short_insight_job(db_path, datetime(2026, 4, 15, 10, 15, tzinfo=UTC))

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "1h"
    ] == ["2026-04-15T09:00:00Z"]


def _enqueue_summary_job(
    db_path: Path,
    *,
    summary_type: SummaryType,
    period_start: datetime,
    period_end: datetime,
) -> str:
    start_iso = iso_z(period_start)
    return enqueue_local_activity_summary_job(
        payload={
            "job_id": f"job-{summary_type}-{start_iso}",
            "process_id": f"process-{summary_type}-{start_iso}",
            "summary_id": f"summary-{summary_type}-{start_iso}",
            "user_id": "user-1",
            "enqueued_at": start_iso,
            "summary_type": summary_type,
            "period_start": start_iso,
            "period_end": iso_z(period_end),
        },
        db_path=db_path,
        busy_timeout_ms=1_000,
    )["job_id"]


@pytest.mark.asyncio
async def test_daily_summary_waits_for_the_days_hourly_summary_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A day summarized before its hours land loses those hours for good."""
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    job_id = _enqueue_summary_job(
        db_path,
        summary_type="1h",
        period_start=datetime(2026, 4, 14, 9, 0, tzinfo=UTC),
        period_end=datetime(2026, 4, 14, 10, 0, tzinfo=UTC),
    )
    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)

    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [call for call in enqueue_calls if call["summary_type"] == "24h"] == []
    assert _read_scheduler_job_cursor_values(db_path)["activity_summary_24h"] is None
    assert _read_scheduler_job_runs(db_path, "activity_summary_24h") == []

    _finalize_job(db_path, job_id)
    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "24h"
    ] == ["2026-04-14T00:00:00Z"]
    assert (
        _read_scheduler_job_cursor_values(db_path)["activity_summary_24h"]
        == "2026-04-14T00:00:00Z"
    )


@pytest.mark.asyncio
async def test_daily_summary_ignores_an_hourly_job_from_another_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _enqueue_summary_job(
        db_path,
        summary_type="1h",
        period_start=datetime(2026, 4, 15, 8, 0, tzinfo=UTC),
        period_end=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
    )

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "24h"
    ] == ["2026-04-14T00:00:00Z"]


@pytest.mark.asyncio
async def test_daily_summary_waits_for_a_short_insight_run_inside_the_day(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The 1h scheduler withholds the hour's job while its Insight run is open."""
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _enqueue_short_insight_job(db_path, datetime(2026, 4, 14, 9, 56, tzinfo=UTC))

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [call for call in enqueue_calls if call["summary_type"] == "24h"] == []
    assert _read_scheduler_job_cursor_values(db_path)["activity_summary_24h"] is None
    assert _read_scheduler_job_runs(db_path, "activity_summary_24h") == []


@pytest.mark.asyncio
async def test_weekly_summary_waits_for_a_daily_summary_job_inside_the_week(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A week summarized before its days land loses those days for good."""
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    job_id = _enqueue_summary_job(
        db_path,
        summary_type="24h",
        period_start=datetime(2026, 4, 8, tzinfo=UTC),
        period_end=datetime(2026, 4, 9, tzinfo=UTC),
    )
    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)

    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [call for call in enqueue_calls if call["summary_type"] == "1w"] == []
    assert _read_scheduler_job_cursor_values(db_path)["activity_summary_1w"] is None
    assert _read_scheduler_job_runs(db_path, "activity_summary_1w") == []

    _finalize_job(db_path, job_id)
    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "1w"
    ] == ["2026-04-06T00:00:00Z"]


@pytest.mark.asyncio
async def test_weekly_summary_waits_for_an_hourly_summary_job_inside_the_week(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The day withholds its own job for that hour, so no day job is queued yet."""
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _enqueue_summary_job(
        db_path,
        summary_type="1h",
        period_start=datetime(2026, 4, 8, 9, tzinfo=UTC),
        period_end=datetime(2026, 4, 8, 10, tzinfo=UTC),
    )

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [call for call in enqueue_calls if call["summary_type"] == "1w"] == []
    assert _read_scheduler_job_cursor_values(db_path)["activity_summary_1w"] is None


@pytest.mark.asyncio
async def test_weekly_summary_ignores_a_daily_summary_job_from_another_week(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _enqueue_summary_job(
        db_path,
        summary_type="24h",
        period_start=datetime(2026, 4, 14, tzinfo=UTC),
        period_end=datetime(2026, 4, 15, tzinfo=UTC),
    )

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "1w"
    ] == ["2026-04-06T00:00:00Z"]


@pytest.mark.asyncio
async def test_scheduler_skips_the_backlog_older_than_the_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The first tick after a long stop enqueues no window older than the limit.

    Walking a month of windows one tick at a time takes hours, and no summary of
    the current period comes out meanwhile. Every type jumps to its newest
    summarizable window and resumes its ordinary walk from there.
    """
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    for job_name, cursor_value in (
        ("activity_summary_1h", "2026-03-15T00:00:00Z"),
        ("activity_summary_24h", "2026-03-15T00:00:00Z"),
        ("activity_summary_1w", "2026-03-02T00:00:00Z"),
        ("activity_summary_1m", "2026-01-01T00:00:00Z"),
        ("activity_summary_3m", "2025-07-01T00:00:00Z"),
    ):
        _seed_scheduler_cursor(db_path, job_name, cursor_value)
    now = datetime(2026, 4, 15, 10, 35, tzinfo=UTC)

    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [
        (str(call["summary_type"]), str(call["period_start"])) for call in enqueue_calls
    ] == [
        ("1h", "2026-04-14T10:00:00Z"),
        ("24h", "2026-04-14T00:00:00Z"),
        ("1w", "2026-04-06T00:00:00Z"),
        ("1m", "2026-03-01T00:00:00Z"),
        ("3m", "2026-01-01T00:00:00Z"),
    ]

    await run_activity_summary_scheduler_once(
        db_path=db_path, busy_timeout_ms=1_000, now=now
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "1h"
    ] == ["2026-04-14T10:00:00Z", "2026-04-14T11:00:00Z"]


@pytest.mark.asyncio
async def test_scheduler_leaves_behind_a_short_insight_run_older_than_the_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An open run inside an expired window no longer withholds later windows.

    Only one short Insight job is active per user, so a single job left queued
    while there is no route used to withhold the summary of its hour, and every
    hour after it, for good.
    """
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _seed_scheduler_cursor(db_path, "activity_summary_1h", "2026-03-15T09:00:00Z")
    _enqueue_short_insight_job(db_path, datetime(2026, 3, 15, 10, 15, tzinfo=UTC))

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "1h"
    ] == ["2026-04-14T10:00:00Z"]


@pytest.mark.asyncio
async def test_daily_summary_leaves_behind_an_hourly_job_older_than_the_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An hourly job inside an expired window no longer withholds later days."""
    db_path, enqueue_calls = _scheduler_fixture(monkeypatch, tmp_path)
    _seed_scheduler_cursor(db_path, "activity_summary_24h", "2026-03-14T00:00:00Z")
    _enqueue_summary_job(
        db_path,
        summary_type="1h",
        period_start=datetime(2026, 3, 15, 9, tzinfo=UTC),
        period_end=datetime(2026, 3, 15, 10, tzinfo=UTC),
    )

    await run_activity_summary_scheduler_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        now=datetime(2026, 4, 15, 10, 35, tzinfo=UTC),
    )

    assert [
        str(call["period_start"])
        for call in enqueue_calls
        if call["summary_type"] == "24h"
    ] == ["2026-04-14T00:00:00Z"]

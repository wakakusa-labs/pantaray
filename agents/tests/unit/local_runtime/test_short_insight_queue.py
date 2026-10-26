from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from pantaray_agents.local_runtime.context import store
from pantaray_agents.local_runtime.context.source_control import context_source_control
from pantaray_agents.local_runtime.context.source_gate import (
    ActiveSource,
    SourceInvalidated,
)
from pantaray_agents.local_runtime.context.source_protocol import PageResponse
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.identity import (
    register_logged_out_owner,
    reset_logged_out_owner,
)
from pantaray_agents.local_runtime.runtime.insight_queue import (
    SHORT_INSIGHT_WINDOW_SECONDS,
    build_local_insight_enqueue_request,
    build_short_insight_job_payload,
    enqueue_due_short_insight_job,
    short_insight_window_start,
)
from pantaray_agents.local_runtime.runtime.job_enqueue import (
    enqueue_local_job_with_connection,
)
from pantaray_agents.local_runtime.runtime.local_worker import (
    LocalJobExecutionError,
    build_default_local_worker_specs,
    run_next_local_job,
)
from pantaray_agents.local_runtime.runtime.session_store import (
    import_desktop_session,
    mark_configured,
    reset_desktop_session_store,
)
from pantaray_agents.local_runtime.runtime.short_insight import run_short_insight
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
from pantaray_agents.schema.context_source import SourceBinding, SourceReady
from pantaray_agents.tasks.types import InsightJobPayload

from .migrated_db import prepare_test_database

USER = "user-1"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    prepare_test_database(path, 1_000, load_default_migrations())
    with open_memory_catalog_connection(db_path=path, busy_timeout_ms=1_000) as conn:
        conn.execute("INSERT INTO users VALUES (?, 'ja', NULL, 'now', 'now')", (USER,))
        conn.commit()
    return path


def _persist_ready_source(db_path: Path, *, capture_paused: bool) -> SourceBinding:
    binding = SourceBinding(
        user_id=USER,
        epoch=UUID(int=1),
        policy_revision="policy-1",
        store_id="store-1",
        protocol_version=1,
    )
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        with immediate_transaction(conn):
            store.compare_source(
                conn,
                USER,
                store.get_source(conn, USER),
                SourceReady(
                    kind="ready", binding=binding, capture_paused=capture_paused
                ),
            )
    return binding


@pytest.fixture
async def active_source(db_path: Path) -> None:
    binding = _persist_ready_source(db_path, capture_paused=False)
    async with context_source_control.gate.turn():
        context_source_control.gate.activate(binding)
    yield
    async with context_source_control.gate.turn():
        context_source_control.gate.revoke(USER)


@pytest.fixture(autouse=True)
def _reset_session_store() -> Iterator[None]:
    reset_desktop_session_store()
    # Startup registers the logged-out owner before any job runs, and the route
    # identity a claimed job records is derived from it (design 7.1).
    register_logged_out_owner("local-owner")
    yield
    reset_logged_out_owner()
    reset_desktop_session_store()


def _jobs(db_path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.execute(
                "SELECT job_id, logical_key, status FROM jobs ORDER BY scheduled_at"
            )
        ]


def test_a_window_never_crosses_a_clock_hour() -> None:
    # The 1h Activity Summary selects logs whose whole period is inside one hour.
    moment = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    for offset in range(0, 7200, 17):
        start = short_insight_window_start(moment + timedelta(seconds=offset))
        end = start + timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS)
        hour_start = start.replace(minute=0, second=0, microsecond=0)
        assert hour_start <= start
        assert end <= hour_start + timedelta(hours=1)


def test_the_run_identity_is_stable_within_a_window() -> None:
    early = build_short_insight_job_payload(
        user_id=USER,
        window_start=short_insight_window_start(
            datetime(2026, 9, 7, 10, 1, tzinfo=UTC)
        ),
    )
    late = build_short_insight_job_payload(
        user_id=USER,
        window_start=short_insight_window_start(
            datetime(2026, 9, 7, 10, 14, 59, tzinfo=UTC)
        ),
    )
    other_window = build_short_insight_job_payload(
        user_id=USER,
        window_start=short_insight_window_start(
            datetime(2026, 9, 7, 10, 15, tzinfo=UTC)
        ),
    )
    other_user = build_short_insight_job_payload(
        user_id="user-2",
        window_start=short_insight_window_start(
            datetime(2026, 9, 7, 10, 1, tzinfo=UTC)
        ),
    )

    assert early == late
    assert early["period_start"] == "2026-09-07T10:00:00Z"
    assert early["period_end"] == "2026-09-07T10:15:00Z"
    assert other_window["insight_id"] != early["insight_id"]
    assert other_user["insight_id"] != early["insight_id"]


async def test_enqueue_is_skipped_without_a_current_source(db_path: Path) -> None:
    result = await enqueue_due_short_insight_job(
        db_path=db_path, busy_timeout_ms=1_000, user_id=USER
    )

    assert result is None
    assert _jobs(db_path) == []


async def test_paused_capture_enqueues_no_run(
    db_path: Path, active_source: None
) -> None:
    """Recording turned off keeps the permit for reads but produces no Suggestion."""
    del active_source
    _persist_ready_source(db_path, capture_paused=True)

    result = await enqueue_due_short_insight_job(
        db_path=db_path, busy_timeout_ms=1_000, user_id=USER
    )

    assert result is None
    assert _jobs(db_path) == []


async def test_a_repeated_tick_in_one_window_enqueues_one_job(
    db_path: Path, active_source: None
) -> None:
    del active_source
    now = datetime(2026, 9, 7, 10, 1, tzinfo=UTC)

    first = await enqueue_due_short_insight_job(
        db_path=db_path, busy_timeout_ms=1_000, user_id=USER, now=now
    )
    second = await enqueue_due_short_insight_job(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        now=now + timedelta(seconds=90),
    )

    assert first is not None and first["inserted_new"] is True
    assert second == {
        "job_id": first["job_id"],
        "process_id": first["process_id"],
        "inserted_new": False,
    }
    assert _jobs(db_path) == [(first["job_id"], USER, "queued")]


async def test_a_later_window_waits_for_the_active_run(
    db_path: Path, active_source: None
) -> None:
    del active_source
    now = datetime(2026, 9, 7, 10, 1, tzinfo=UTC)

    first = await enqueue_due_short_insight_job(
        db_path=db_path, busy_timeout_ms=1_000, user_id=USER, now=now
    )
    later = await enqueue_due_short_insight_job(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        now=now + timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS),
    )

    assert first is not None
    assert later is not None
    assert later["job_id"] == first["job_id"]
    assert later["inserted_new"] is False
    assert len(_jobs(db_path)) == 1


def test_a_run_that_loses_its_source_releases_the_next_window(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation must terminate the job; `logical_key` blocks every later window."""
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version="1",
    )
    # The worker only claims once Electron main has applied `configure`.
    mark_configured()
    window_start = datetime(2025, 1, 6, 10, 0, tzinfo=UTC)
    first = _enqueue_window(db_path, window_start)

    def _revoked_source(_payload: object) -> None:
        raise SourceInvalidated("context source was invalidated")

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.local_worker.run_insight_job",
        _revoked_source,
    )
    with pytest.raises(LocalJobExecutionError, match="SourceInvalidated"):
        run_next_local_job(
            db_path=db_path,
            busy_timeout_ms=1_000,
            claimed_by="test-local-worker",
            specs=build_default_local_worker_specs(action_runner=lambda _: None),
        )

    later = _enqueue_window(
        db_path, window_start + timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS)
    )

    assert later != first
    assert _jobs(db_path) == [(first, USER, "failed"), (later, USER, "queued")]


def _enqueue_window(db_path: Path, window_start: datetime) -> str:
    return enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_insight_enqueue_request(
            build_short_insight_job_payload(user_id=USER, window_start=window_start)
        ),
    )["job_id"]


async def test_upgrade_can_enqueue_after_a_completed_four_minute_window(
    db_path: Path, active_source: None
) -> None:
    del active_source
    now = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    payload = build_short_insight_job_payload(user_id=USER, window_start=now)
    old_namespace = uuid5(NAMESPACE_URL, "pantaray:short_insight:v1")
    for key, kind in (
        ("job_id", "job"),
        ("process_id", "process"),
        ("insight_id", "insight"),
    ):
        payload[key] = str(
            uuid5(old_namespace, f"{kind}:{USER}:{payload['period_start']}")
        )
    payload["period_end"] = "2026-09-07T10:04:00Z"
    old_job = enqueue_local_job_with_connection(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        request=build_local_insight_enqueue_request(payload),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE job_id = ?",
            (old_job["job_id"],),
        )

    new_job = await enqueue_due_short_insight_job(
        db_path=db_path, busy_timeout_ms=1_000, user_id=USER, now=now
    )

    assert new_job is not None and new_job["inserted_new"]
    assert new_job["job_id"] != old_job["job_id"]
    assert len(_jobs(db_path)) == 2


class _EmptyTimeline:
    """A recorder that holds nothing after the stored cursor."""

    def __init__(self) -> None:
        self.reads = 0

    async def read_page(self, _source: ActiveSource, _request: object) -> PageResponse:
        self.reads += 1
        return PageResponse.model_validate(
            {
                "protocol_version": 1,
                "kind": "page",
                "store_identity": "store-1",
                "observations": (),
                "next_cursor": "cursor-1",
                "upper_bound": "upper-1",
                "has_more": False,
                "coverage": {"after": 0, "through": 0},
            }
        )


class _NoModelCall:
    async def generate(self, **_kwargs: object) -> None:
        raise AssertionError("an idle window must not call the model")


async def test_an_idle_window_runs_once_and_releases_the_next_window(
    db_path: Path, active_source: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skipped run completes its job, so a later tick neither repeats nor waits."""
    del active_source
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        desktop_access_token="header.payload.signature",
        expires_at="2099-03-27T01:00:00Z",
        session_version="1",
    )
    mark_configured()
    timeline = _EmptyTimeline()

    def _run_insight_job(payload: InsightJobPayload) -> None:
        asyncio.run(
            run_short_insight(
                db_path=db_path,
                busy_timeout_ms=1_000,
                user_id=payload["user_id"],
                run_id=payload["insight_id"],
                period_start=payload["period_start"],
                period_end=payload["period_end"],
                gate=context_source_control.gate,
                reader=timeline,  # type: ignore[arg-type]
                agent=_NoModelCall(),  # type: ignore[arg-type]
            )
        )

    monkeypatch.setattr(
        "pantaray_agents.local_runtime.runtime.local_worker.run_insight_job",
        _run_insight_job,
    )

    def _work() -> bool:
        return run_next_local_job(
            db_path=db_path,
            busy_timeout_ms=1_000,
            claimed_by="test-local-worker",
            specs=build_default_local_worker_specs(action_runner=lambda _: None),
        )

    now = datetime(2026, 10, 23, 19, 1, tzinfo=UTC)
    first = await enqueue_due_short_insight_job(
        db_path=db_path, busy_timeout_ms=1_000, user_id=USER, now=now
    )
    assert first is not None
    assert await asyncio.to_thread(_work)

    repeat = await enqueue_due_short_insight_job(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        now=now + timedelta(seconds=90),
    )
    assert repeat is not None and repeat["inserted_new"] is False
    assert not await asyncio.to_thread(_work)
    assert timeline.reads == 1

    later = await enqueue_due_short_insight_job(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER,
        now=now + timedelta(seconds=SHORT_INSIGHT_WINDOW_SECONDS),
    )
    assert later is not None and later["inserted_new"] is True
    assert _jobs(db_path) == [
        (first["job_id"], USER, "completed"),
        (later["job_id"], USER, "queued"),
    ]
    with sqlite3.connect(db_path) as connection:
        insights = connection.execute("SELECT COUNT(*) FROM agent_insights").fetchone()
    assert insights == (0,)

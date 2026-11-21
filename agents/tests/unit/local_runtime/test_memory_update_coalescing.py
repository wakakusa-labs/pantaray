"""When the builtin.memory_update hook starts a run, and what one run is handed."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.activity_log_seed import seed_activity_log

from pantaray_agents.local_runtime.hooks.events import (
    ActionTurnEnded,
    InsightRecorded,
)
from pantaray_agents.local_runtime.runtime.activity_source_rows import (
    persist_activity_summary_result,
    reserve_activity_summary_processing,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    MEMORY_UPDATE_SOURCE_MAX_ITEMS,
)
from pantaray_agents.local_runtime.runtime.session_store import import_desktop_session
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)

from .memory_update_hook import (
    Tick,
    finish_memory_jobs,
    memory_payloads,
    record,
    tick,
)
from .migrated_db import prepare_test_database

__all__ = ["tick"]

FIRST_AT = "2026-09-07T00:00:00Z"
NOW = datetime(2026, 9, 7, 0, 10, tzinfo=UTC)
# One 15-minute Insight window after the first event.
WINDOW_ELAPSED = datetime(2026, 9, 7, 0, 15, tzinfo=UTC)


def _prepare_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )
    return db_path


def _insights(db_path: Path, count: int, *, at: str = FIRST_AT) -> list[str]:
    ids = [f"insight-{index}-{at}" for index in range(count)]
    for insight_id in ids:
        record(db_path, InsightRecorded(insight_id=insight_id), at=at)
    return ids


def _turn(db_path: Path, source_id: str = "turn-1") -> None:
    record(
        db_path,
        ActionTurnEnded(
            source_id=source_id,
            action_id="action-1",
            action_completed_at=FIRST_AT,
            source_action_revision_id="revision-1",
            turn_start_step_number=3,
            turn_end_step_number=9,
            action_prompt_name="action",
            action_prompt_version="2.0",
            suggestion_id=None,
        ),
        at=FIRST_AT,
    )


def _pending(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM hook_inbox").fetchone()[0])


def test_an_insight_starts_one_run_at_once_with_what_rode_along(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches an Insight waiting like a ride-along, an Action turn left behind
    # by the run it rode with, and handed events left pending to be handed again.
    db_path = _prepare_db(tmp_path)
    _turn(db_path)
    insights = _insights(db_path, 3)

    tick(db_path, datetime.fromisoformat(FIRST_AT))
    tick(db_path, NOW)

    (payload,) = memory_payloads(db_path)
    assert payload["short_insight_ids"] == insights
    assert payload["action_terminals"] == [
        {
            "source_id": "turn-1",
            "action_id": "action-1",
            "action_completed_at": FIRST_AT,
            "turn_start_step_number": 3,
            "turn_end_step_number": 9,
            "action_prompt_name": "action",
            "action_prompt_version": "2.0",
            "source_action_revision_id": "revision-1",
        }
    ]
    assert _pending(db_path) == 0


def test_an_action_turn_alone_waits_one_insight_window(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches a finished turn starting a run of its own at once, and one that
    # never starts when no Insight comes (recording off).
    db_path = _prepare_db(tmp_path)
    _turn(db_path)

    tick(db_path, NOW)
    assert memory_payloads(db_path) == []
    tick(db_path, WINDOW_ELAPSED)
    assert [len(p["action_terminals"]) for p in memory_payloads(db_path)] == [1]


def test_events_during_a_run_go_to_the_next_single_run(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches a second run beside the active one, and events that arrive
    # meanwhile lost or split over several runs.
    db_path = _prepare_db(tmp_path)
    first = _insights(db_path, 1)
    tick(db_path, NOW)
    _turn(db_path)
    later = _insights(db_path, 2, at="2026-09-07T00:11:00Z")
    tick(db_path, NOW + timedelta(minutes=2))
    assert len(memory_payloads(db_path)) == 1

    finish_memory_jobs(db_path)
    tick(db_path, NOW + timedelta(minutes=3))
    tick(db_path, NOW + timedelta(minutes=4))

    payloads = memory_payloads(db_path)
    assert [p["short_insight_ids"] for p in payloads] == [first, later]
    assert [len(p["action_terminals"]) for p in payloads] == [0, 1]


def test_a_backlog_beyond_one_run_waits_for_the_next(
    tmp_path: Path, tick: Tick
) -> None:
    # Catches a run handed more than its payload allows (the job is refused),
    # and the rest consumed with it instead of reaching the next run.
    db_path = _prepare_db(tmp_path)
    insights = _insights(db_path, MEMORY_UPDATE_SOURCE_MAX_ITEMS + 2)

    tick(db_path, NOW)
    finish_memory_jobs(db_path)
    tick(db_path, NOW + timedelta(minutes=1))

    assert [p["short_insight_ids"] for p in memory_payloads(db_path)] == [
        insights[:MEMORY_UPDATE_SOURCE_MAX_ITEMS],
        insights[MEMORY_UPDATE_SOURCE_MAX_ITEMS:],
    ]


def test_a_failed_enqueue_consumes_nothing(
    tmp_path: Path, tick: Tick, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches events consumed outside the enqueue's transaction: a crash or a
    # failed write there would lose them for good.
    db_path = _prepare_db(tmp_path)
    insights = _insights(db_path, 1)

    def fail(**_kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(
            "pantaray_agents.local_runtime.runtime.memory_update_queue.enqueue_local_job",
            fail,
        )
        tick(db_path, NOW)
    assert (memory_payloads(db_path), _pending(db_path)) == ([], 1)

    tick(db_path, NOW + timedelta(minutes=1))
    assert [p["short_insight_ids"] for p in memory_payloads(db_path)] == [insights]


def _persist_summary(db_path: Path, *, summary_id: str, summary_type: str) -> None:
    period_start = (
        "2026-08-15T00:00:00Z" if summary_type == "24h" else "2026-08-15T23:00:00Z"
    )
    common = {
        "db_path": db_path,
        "busy_timeout_ms": 1_000,
        "summary_id": summary_id,
        "user_id": "user-1",
        "summary_type": summary_type,
        "period_start": period_start,
        "period_end": "2026-08-16T00:00:00Z",
    }
    reserve_activity_summary_processing(**common, created_at="2026-08-16T00:10:00Z")
    persist_activity_summary_result(
        **common,
        summary="summary",
        status="success",
        error=None,
        prompt_text="prompt",
        thinking=None,
        source_ids=["log-1"],
        updated_at="2026-08-16T00:10:01Z",
    )


def test_only_a_daily_summary_starts_a_run(tmp_path: Path, tick: Tick) -> None:
    # Catches an hourly summary starting a Memory update, and a daily one that
    # starts none or waits like a ride-along.
    db_path = _prepare_db(tmp_path)
    seed_activity_log(
        db_path,
        log_id="log-1",
        user_id="user-1",
        period_start="2026-08-15T23:56:00Z",
        period_end="2026-08-16T00:00:00Z",
    )
    _persist_summary(db_path, summary_id="summary-1h", summary_type="1h")
    tick(db_path, datetime(2026, 8, 16, 0, 11, tzinfo=UTC))
    assert (memory_payloads(db_path), _pending(db_path)) == ([], 0)

    _persist_summary(db_path, summary_id="summary-24h", summary_type="24h")
    tick(db_path, datetime(2026, 8, 16, 0, 11, tzinfo=UTC))
    assert [p["summary_ids"] for p in memory_payloads(db_path)] == [["summary-24h"]]

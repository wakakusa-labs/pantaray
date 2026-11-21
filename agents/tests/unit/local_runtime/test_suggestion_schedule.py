"""When builtin.suggestion queues a Suggestion, and when its job may start one."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.hooks import gates, runner
from pantaray_agents.local_runtime.hooks.builtin import builtin_hooks
from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime import suggestion_from_insight
from pantaray_agents.local_runtime.runtime.job_claim import (
    claim_next_pending_job,
    finalize_local_job,
)
from pantaray_agents.local_runtime.runtime.job_types import LOCAL_SUGGESTION_JOB_TYPE
from pantaray_agents.local_runtime.runtime.suggestion_from_insight import (
    read_reconsidered_insight,
    request_suggestion_in_connection,
    reserve_suggestion_start,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.storage.transactions import immediate_transaction

from .migrated_db import prepare_test_database

NOW = datetime(2026, 9, 7, tzinfo=UTC)
INTERVAL = timedelta(minutes=15)


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hooks = tuple(hook for hook in builtin_hooks() if hook.id == "builtin.suggestion")
    monkeypatch.setattr(runner, "builtin_hooks", lambda: hooks)
    monkeypatch.setattr(gates, "owner_is_settled", lambda: True)
    path = tmp_path / "runtime.db"
    prepare_test_database(path, 1_000, load_default_migrations())
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO users(user_id,ui_language,created_at,updated_at) VALUES (?,'ja',?,?)",
            [(user, NOW.isoformat(), NOW.isoformat()) for user in ("user-1", "user-2")],
        )
    return path


def _request(
    db_path: Path, *insight_ids: str, user_id: str = "user-1", at: datetime = NOW
) -> None:
    """What a finished Memory update records in its completion transaction."""
    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        with immediate_transaction(conn):
            request_suggestion_in_connection(
                conn,
                user_id=user_id,
                insight_ids=insight_ids,
                requested_at=format_utc_iso(at),
            )


def _tick(db_path: Path, now: datetime, user_id: str = "user-1") -> None:
    runner.run_hooks_once(
        db_path=db_path, busy_timeout_ms=1_000, user_id=user_id, now=now
    )


def _queued(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT payloads.payload_json FROM jobs
               JOIN job_payloads AS payloads USING(job_id)
               WHERE jobs.job_type = ? ORDER BY jobs.rowid""",
            (LOCAL_SUGGESTION_JOB_TYPE,),
        ).fetchall()
    return [json.loads(row[0])["insight_id"] for row in rows]


def _pending(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM hook_inbox WHERE event_name = 'suggestion.requested'"
            ).fetchone()[0]
        )


def _claim(db_path: Path, user_id: str = "user-1") -> dict[str, str]:
    claimed = claim_next_pending_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_type=LOCAL_SUGGESTION_JOB_TYPE,
        owner_user_id=user_id,
        claimed_by="worker",
        process_running_status="running",
        expected_process_pending_status="enqueued",
    )
    assert claimed is not None
    return json.loads(claimed["payload_json"])


def _reserve(db_path: Path, payload: dict[str, str], now: datetime):
    return reserve_suggestion_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=payload["user_id"],
        process_id=payload["process_id"],
        now=now,
    )


def _finish(db_path: Path, payload: dict[str, str]) -> None:
    finalize_local_job(
        db_path=str(db_path),
        busy_timeout_ms=1_000,
        job_id=payload["job_id"],
        final_status="completed",
        process_final_status="completed",
    )


def _start_one(db_path: Path, insight_id: str, at: datetime) -> None:
    """A Suggestion that starts its generation at ``at`` and finishes."""
    _request(db_path, insight_id, at=at)
    _tick(db_path, at)
    job = _claim(db_path)
    assert _reserve(db_path, job, at) == "start"
    _finish(db_path, job)


def test_a_memory_update_without_an_insight_asks_for_nothing(db_path: Path) -> None:
    # Catches a Suggestion after every Memory update (an Action turn or a chat
    # message alone), and one made from an older Insight than the update's newest.
    _request(db_path)
    _tick(db_path, NOW)
    assert _queued(db_path) == []

    _request(db_path, "older", "newest")
    _tick(db_path, NOW)
    assert _queued(db_path) == ["newest"]


def test_requests_while_one_runs_leave_only_the_newest(db_path: Path) -> None:
    # Catches two Suggestions running at once, an older review handed instead
    # of the newest, and superseded requests left pending to run again later.
    _request(db_path, "first")
    _request(db_path, "second")
    _tick(db_path, NOW)
    assert _queued(db_path) == ["second"]
    assert _pending(db_path) == 0

    running = _claim(db_path)
    assert _reserve(db_path, running, NOW) == "start"
    _request(db_path, "third", at=NOW + timedelta(minutes=1))
    _request(db_path, "fourth", at=NOW + timedelta(minutes=2))
    _tick(db_path, NOW + INTERVAL)
    assert _queued(db_path) == ["second"]

    _finish(db_path, running)
    _tick(db_path, NOW + INTERVAL)
    assert _queued(db_path) == ["second", "fourth"]
    assert _pending(db_path) == 0


def test_a_request_inside_the_interval_waits_for_it(db_path: Path) -> None:
    # Catches a start sooner than one window after the previous generation
    # start, and a request dropped instead of deferred.
    _start_one(db_path, "first", NOW)

    _request(db_path, "second", at=NOW + timedelta(minutes=5))
    _tick(db_path, NOW + timedelta(minutes=5))
    _tick(db_path, NOW + INTERVAL - timedelta(seconds=1))
    assert _queued(db_path) == ["first"]

    _tick(db_path, NOW + INTERVAL)
    assert _queued(db_path) == ["first", "second"]
    assert _reserve(db_path, _claim(db_path), NOW + INTERVAL) == "start"


def test_a_retry_counts_toward_the_interval_and_yields_to_a_newer_request(
    db_path: Path,
) -> None:
    # Catches a retry that generates again at once (each retry is a full model
    # run), and a retry that outlives a newer review instead of giving way.
    _request(db_path, "first")
    _tick(db_path, NOW)
    first = _claim(db_path)
    assert _reserve(db_path, first, NOW) == "start"
    # The run fails after entering the agent and is requeued.
    assert _reserve(db_path, first, NOW + timedelta(seconds=5)) == NOW + INTERVAL

    _request(db_path, "second", at=NOW + timedelta(minutes=3))
    _tick(db_path, NOW + timedelta(minutes=3))
    assert _queued(db_path) == ["first"]
    assert _reserve(db_path, first, NOW + INTERVAL) == "superseded"

    _finish(db_path, first)
    _tick(db_path, NOW + INTERVAL)
    assert _queued(db_path) == ["first", "second"]
    assert _reserve(db_path, _claim(db_path), NOW + INTERVAL) == "start"


def test_a_failed_enqueue_neither_loses_nor_doubles_the_request(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches a job committed without consuming its request (a second
    # Suggestion after a restart) or a request consumed without its job.
    _request(db_path, "first")

    def crash(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(suggestion_from_insight, "consume_in_connection", crash)
        _tick(db_path, NOW)
    assert (_queued(db_path), _pending(db_path)) == ([], 1)

    for _ in range(2):
        _tick(db_path, NOW + timedelta(seconds=3))
    assert (_queued(db_path), _pending(db_path)) == (["first"], 0)


def test_another_users_start_or_request_does_not_touch_this_user(
    db_path: Path,
) -> None:
    # Catches the interval or the supersession read across users.
    _request(db_path, "first-user")
    _tick(db_path, NOW)
    first = _claim(db_path)
    _request(db_path, "second-user", user_id="user-2")
    _tick(db_path, NOW, user_id="user-2")
    second = _claim(db_path, "user-2")
    assert _reserve(db_path, second, NOW) == "start"
    _request(db_path, "second-user-again", user_id="user-2")
    assert _reserve(db_path, first, NOW) == "start"


def test_unchanged_insight_is_a_valid_periodic_review_input(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_insights(insight_id,user_id,status,
               short_term_insight_data,facts,prompt_name,prompt_version,created_at,updated_at)
               VALUES ('insight-1','user-1','success','Still working on the proposal.',
                       '','insight','2.2',?,?)""",
            (NOW.isoformat(), NOW.isoformat()),
        )
    source = read_reconsidered_insight(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        insight_id="insight-1",
    )
    assert source.short_term_insight == "Still working on the proposal."
    assert "Periodic review" in source.reconsideration_reason


def test_the_welcome_neither_delays_nor_supersedes_the_first_real_suggestion(
    db_path: Path,
) -> None:
    from pantaray_agents.local_runtime.runtime.welcome_suggestion import (
        record_welcome_suggestion,
    )

    with open_memory_catalog_connection(db_path=db_path, busy_timeout_ms=1_000) as conn:
        assert record_welcome_suggestion(
            connection=conn,
            user_id="user-1",
            answer="Welcome",
            now=NOW.isoformat().replace("+00:00", "Z"),
        )
    later = NOW + timedelta(minutes=5)
    _request(db_path, "first-insight", at=later)
    _tick(db_path, later)
    # Five minutes after the welcome, the first real suggestion starts at once.
    assert _reserve(db_path, _claim(db_path), later) == "start"

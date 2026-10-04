from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pantaray_agents.local_runtime.runtime.memory_agent_dispatcher import (
    dispatch_memory_agent_triggers_once,
)
from pantaray_agents.local_runtime.runtime.session_store import import_desktop_session
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)

from .migrated_db import prepare_test_database

USER_ID = "user-1"
FIRST_TRIGGER_AT = "2026-09-07T00:00:00Z"
NOW = datetime(2026, 9, 7, 0, 10, tzinfo=UTC)
# One 15-minute Insight window after the first trigger.
ACTION_DEFERRAL_ELAPSED = datetime(2026, 9, 7, 0, 15, tzinfo=UTC)


def _prepare_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, 1_000, load_default_migrations())
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER_ID,
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )
    return db_path


def _insert_short_insight_triggers(db_path: Path, count: int) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executemany(
            """
            INSERT INTO memory_agent_triggers(
                user_id, trigger_kind, source_id, status, created_at
            ) VALUES (?, 'memory_from_short_insight', ?, 'pending', ?)
            """,
            [(USER_ID, f"insight-{index}", FIRST_TRIGGER_AT) for index in range(count)],
        )


def _insert_action_terminal_trigger(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO memory_agent_triggers(
                user_id, trigger_kind, source_id, status, created_at, action_id,
                action_completed_at, source_action_revision_id,
                turn_start_step_number, turn_end_step_number,
                action_prompt_name, action_prompt_version, suggestion_id
            ) VALUES (?, 'memory_from_action_terminal', 'turn-1', 'pending', ?,
                      'action-1', ?, 'revision-1', 3, 9, 'action', '2.0',
                      'suggestion-1')
            """,
            (USER_ID, FIRST_TRIGGER_AT, FIRST_TRIGGER_AT),
        )


def _dispatch(db_path: Path, now: datetime) -> object:
    return dispatch_memory_agent_triggers_once(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id=USER_ID,
        now=now,
    )


def _memory_update_jobs(db_path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT job_id, logical_key, status
            FROM jobs WHERE job_type = 'memory_update'
            """
        ).fetchall()


def test_pending_short_insights_start_one_run_without_waiting_for_a_batch(
    tmp_path: Path,
) -> None:
    db_path = _prepare_db(tmp_path)
    _insert_short_insight_triggers(db_path, 3)

    result = _dispatch(db_path, NOW)

    jobs = _memory_update_jobs(db_path)
    assert len(jobs) == 1
    job_id, logical_key, status = jobs[0]
    assert (logical_key, status) == (USER_ID, "queued")
    assert len(result.outcomes) == 3
    assert {outcome.job_id for outcome in result.outcomes} == {job_id}
    with sqlite3.connect(db_path) as connection:
        statuses = connection.execute(
            """
            SELECT status, dispatched_job_id FROM memory_agent_triggers
            WHERE trigger_kind = 'memory_from_short_insight'
            """
        ).fetchall()
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
        )
    assert statuses == [("dispatched", job_id)] * 3
    assert sorted(payload["short_insight_ids"]) == sorted(
        f"insight-{index}" for index in range(3)
    )
    assert payload["summary_ids"] == []
    assert payload["action_terminals"] == []


def test_one_fresh_short_insight_starts_memory_immediately(tmp_path: Path) -> None:
    db_path = _prepare_db(tmp_path)
    _insert_short_insight_triggers(db_path, 1)
    result = _dispatch(db_path, datetime.fromisoformat(FIRST_TRIGGER_AT))
    assert len(result.outcomes) == 1
    assert len(_memory_update_jobs(db_path)) == 1


def test_a_fresh_action_terminal_alone_starts_no_run(tmp_path: Path) -> None:
    db_path = _prepare_db(tmp_path)
    _insert_action_terminal_trigger(db_path)

    result = _dispatch(db_path, NOW)

    assert result.outcomes == ()
    assert _memory_update_jobs(db_path) == []


def test_a_fresh_action_terminal_rides_along_with_a_short_insight_run(
    tmp_path: Path,
) -> None:
    db_path = _prepare_db(tmp_path)
    _insert_action_terminal_trigger(db_path)
    _insert_short_insight_triggers(db_path, 1)

    result = _dispatch(db_path, NOW)

    jobs = _memory_update_jobs(db_path)
    assert len(jobs) == 1
    assert {
        (outcome.trigger.trigger_kind, outcome.job_id) for outcome in result.outcomes
    } == {
        ("memory_from_action_terminal", jobs[0][0]),
        ("memory_from_short_insight", jobs[0][0]),
    }
    with sqlite3.connect(db_path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id = ?",
                (jobs[0][0],),
            ).fetchone()[0]
        )
    assert payload["short_insight_ids"] == ["insight-0"]
    assert [item["source_id"] for item in payload["action_terminals"]] == ["turn-1"]


def test_an_action_terminal_runs_alone_after_one_insight_window_with_its_binding(
    tmp_path: Path,
) -> None:
    db_path = _prepare_db(tmp_path)
    _insert_action_terminal_trigger(db_path)

    result = _dispatch(db_path, ACTION_DEFERRAL_ELAPSED)

    jobs = _memory_update_jobs(db_path)
    assert len(jobs) == 1
    assert len(result.outcomes) == 1
    with sqlite3.connect(db_path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM job_payloads WHERE job_id = ?",
                (jobs[0][0],),
            ).fetchone()[0]
        )
    assert payload["action_terminals"] == [
        {
            "source_id": "turn-1",
            "action_id": "action-1",
            "action_completed_at": FIRST_TRIGGER_AT,
            "turn_start_step_number": 3,
            "turn_end_step_number": 9,
            "action_prompt_name": "action",
            "action_prompt_version": "2.0",
            "source_action_revision_id": "revision-1",
            "suggestion_id": "suggestion-1",
        }
    ]


def test_an_active_memory_update_job_blocks_a_second_run(tmp_path: Path) -> None:
    db_path = _prepare_db(tmp_path)
    _insert_action_terminal_trigger(db_path)
    _dispatch(db_path, ACTION_DEFERRAL_ELAPSED)
    _insert_short_insight_triggers(db_path, 3)

    result = _dispatch(db_path, NOW)

    assert result.outcomes == ()
    assert len(_memory_update_jobs(db_path)) == 1
    with sqlite3.connect(db_path) as connection:
        pending = connection.execute(
            """
            SELECT COUNT(*) FROM memory_agent_triggers
            WHERE trigger_kind = 'memory_from_short_insight' AND status = 'pending'
            """
        ).fetchone()
    assert pending == (3,)

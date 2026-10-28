"""Which delivered suggestions are due for a verdict, and verdicts stay appended."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.suggestion_outcomes import (
    JudgedOutcome,
    latest_due_checkpoint,
    load_due_outcomes,
    record_outcome_verdicts,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age", "judged", "expected"),
    [
        (timedelta(days=1) - timedelta(seconds=1), frozenset(), None),
        (timedelta(days=1), frozenset(), 1),
        # Passed 1 and 7 with neither judged: only 7, never a backfilled 1.
        (timedelta(days=8), frozenset(), 7),
        (timedelta(days=8), frozenset({1}), 7),
        (timedelta(days=8), frozenset({7}), None),
        (timedelta(days=31), frozenset({1, 7}), 30),
    ],
)
def test_only_the_latest_passed_checkpoint_is_due(
    age: timedelta, judged: frozenset[int], expected: int | None
) -> None:
    assert (
        latest_due_checkpoint(delivered_at=NOW - age, now=NOW, judged=judged)
        == expected
    )


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(db_path, BUSY_TIMEOUT_MS, load_default_migrations())
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at)"
            " VALUES (?, 'en', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (USER_ID,),
        )
    return db_path


def _deliver(
    db_path: Path,
    suggestion_id: str,
    *,
    age: timedelta,
    summary: str = "",
    has_suggestion: int = 1,
) -> None:
    delivered = format_utc_iso(NOW - age)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, answer, has_suggestion,
                interaction_contract, suggestion_summary, created_at, updated_at
            ) VALUES (?, ?, 'success', 'Try the shorter checklist.', ?,
                      'message_only', ?, ?, ?)
            """,
            (suggestion_id, USER_ID, has_suggestion, summary, delivered, delivered),
        )
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, suggestion_id, started_at,
                updated_at, completed_at, heartbeat_at, next_event_seq
            ) VALUES (?, ?, 'suggestion', 'success', ?, ?, ?, ?, ?, 1)
            """,
            (f"process-{suggestion_id}", USER_ID, suggestion_id, *[delivered] * 4),
        )


def _record(db_path: Path, *outcomes: JudgedOutcome) -> None:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        record_outcome_verdicts(
            connection,
            user_id=USER_ID,
            job_id="job-1",
            prompt_name="memory_update_outcomes",
            prompt_version="1.0",
            judged_at=format_utc_iso(NOW),
            outcomes=outcomes,
        )


def _outcome(suggestion_id: str, checkpoint_days: int) -> JudgedOutcome:
    return JudgedOutcome(
        suggestion_id=suggestion_id,
        checkpoint_days=checkpoint_days,
        verdict="unknown",
        evidence=(),
        reason="The records do not reach the change.",
    )


def _due(db_path: Path) -> list[tuple[str, int]]:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        due = load_due_outcomes(connection, user_id=USER_ID, now=NOW)
    return [(item.suggestion_id, item.checkpoint_days) for item in due]


def test_due_suggestions_are_the_three_earliest_delivered_in_the_window(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    _deliver(db_path, "too-old", age=timedelta(days=46))
    _deliver(db_path, "judged", age=timedelta(days=40))
    _deliver(db_path, "a", age=timedelta(days=35))
    _deliver(db_path, "b", age=timedelta(days=9))
    _deliver(db_path, "not-shown", age=timedelta(days=8), has_suggestion=0)
    _deliver(db_path, "c", age=timedelta(days=3))
    _deliver(db_path, "d", age=timedelta(days=2))
    _deliver(db_path, "not-yet", age=timedelta(hours=20))
    _record(db_path, _outcome("judged", 30))

    assert _due(db_path) == [("a", 30), ("b", 7), ("c", 1)]

    _record(db_path, _outcome("a", 30), _outcome("b", 7))
    assert _due(db_path) == [("c", 1), ("d", 1)]


def test_a_due_item_carries_the_hypothesis_section_and_earlier_verdicts(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    _deliver(
        db_path,
        "s-1",
        age=timedelta(days=8),
        summary="## Why\nSlow reviews.\n### Hypothesis\nReviews close sooner.\n"
        "### Signals\nPR age.",
    )
    _deliver(db_path, "s-2", age=timedelta(days=2), summary="Reviews close sooner.")
    _record(db_path, _outcome("s-1", 1))

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        first, second = load_due_outcomes(connection, user_id=USER_ID, now=NOW)

    assert first.hypothesis == "Reviews close sooner."
    assert [item.checkpoint_days for item in first.earlier_verdicts] == [1]
    assert second.hypothesis == "Reviews close sooner."
    assert second.earlier_verdicts == ()


def test_a_second_verdict_for_a_checkpoint_rolls_back_the_whole_write(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    _deliver(db_path, "s-1", age=timedelta(days=8))
    _deliver(db_path, "s-2", age=timedelta(days=8))
    _record(db_path, _outcome("s-1", 7))

    with pytest.raises(sqlite3.IntegrityError):
        _record(db_path, _outcome("s-2", 7), _outcome("s-1", 7))

    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT suggestion_id FROM suggestion_outcome_verdicts"
        ).fetchall()
    assert stored == [("s-1",)]


def test_verdicts_cannot_be_rewritten_and_go_with_their_suggestion(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    _deliver(db_path, "s-1", age=timedelta(days=8))
    _record(db_path, _outcome("s-1", 7))

    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE suggestion_outcome_verdicts SET verdict = 'improved'"
            )
        connection.execute("DELETE FROM agent_suggestions WHERE suggestion_id = 's-1'")
        remaining = connection.execute(
            "SELECT COUNT(*) FROM suggestion_outcome_verdicts"
        ).fetchone()
    assert tuple(remaining) == (0,)

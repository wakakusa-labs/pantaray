"""Which delivered suggestions and Actions a Memory update looks back on."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.memory_catalog.connection import (
    open_memory_catalog_connection,
)
from pantaray_agents.local_runtime.runtime.help_outcomes import (
    DueOutcome,
    latest_checkpoint_between,
    load_due_outcomes,
)
from pantaray_agents.local_runtime.runtime.memory_update_progress import (
    append_memory_update_completed_in_connection,
)
from pantaray_agents.local_runtime.runtime.utc_timestamps import format_utc_iso
from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.schema.agent.action_message import ActionUserMessageInput
from pantaray_agents.schema.agent.action_message_codec import (
    render_action_user_request_text,
)
from pantaray_agents.tasks.action_user_message import serialize_action_user_message

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
USER_ID = "user-1"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age", "after", "expected"),
    [
        (timedelta(days=1) - timedelta(seconds=1), timedelta(days=45), None),
        (timedelta(days=1), timedelta(days=45), 1),
        # Passed 1 and 7 since the last run: only 7, never a backfilled 1.
        (timedelta(days=8), timedelta(days=45), 7),
        # 7 fell before the last run, which already looked back on it.
        (timedelta(days=8), timedelta(days=1), None),
        (timedelta(days=31), timedelta(days=2), 30),
    ],
)
def test_only_the_latest_checkpoint_since_the_last_run_is_due(
    age: timedelta, after: timedelta, expected: int | None
) -> None:
    assert (
        latest_checkpoint_between(
            delivered_at=NOW - age, after=NOW - after, through=NOW
        )
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
    contract: str = "message_only",
) -> None:
    delivered = format_utc_iso(NOW - age)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, answer, has_suggestion,
                interaction_contract, suggestion_summary, created_at, updated_at
            ) VALUES (?, ?, 'success', 'Try the shorter checklist.', ?, ?, ?, ?, ?)
            """,
            (
                suggestion_id,
                USER_ID,
                has_suggestion,
                contract,
                summary,
                delivered,
                delivered,
            ),
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


def _act(
    db_path: Path,
    action_id: str,
    *,
    request: str,
    turns: tuple[tuple[str, timedelta], ...],
    suggestion_id: str | None = None,
    replied_to: str | None = None,
) -> None:
    """An Action with its turns as (status, age), the first one first."""

    created = format_utc_iso(NOW - turns[0][1])
    message = ActionUserMessageInput(message_id=f"{action_id}-m1", content=request)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id, user_id, suggestion_id, initial_user_message_id,
                execution_target_json, status, final_output, prompt_name,
                prompt_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '{"kind":"scratch"}', 'success', 'done',
                      'action', '1', ?, ?)
            """,
            (action_id, USER_ID, suggestion_id, f"{action_id}-m1", created, created),
        )
        for number, (status, age) in enumerate(turns, start=1):
            at = format_utc_iso(NOW - age)
            connection.execute(
                """
                INSERT INTO processes(
                    process_id, user_id, kind, status, action_id, started_at,
                    updated_at, completed_at, heartbeat_at, next_event_seq
                ) VALUES (?, ?, 'action', ?, ?, ?, ?, ?, ?, 1)
                """,
                (f"{action_id}-p{number}", USER_ID, status, action_id, *[at] * 4),
            )
        first_step = 1
        if replied_to is not None:
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, step_number, local_step_number,
                    short_step_id, step_type, step_name, status, goal_handle,
                    retry_count, prompt_tokens, completion_tokens,
                    llm_response_text, adopted_process_id, source_suggestion_id,
                    started_at, completed_at, created_at
                ) VALUES (?, ?, ?, 1, 1, 'S-1-ASSISTANT', 'assistant_message',
                          'assistant_message', 'success', 'S', 0, 0, 0, 'offer',
                          ?, ?, ?, ?, ?)
                """,
                (
                    f"{action_id}-s1",
                    action_id,
                    USER_ID,
                    f"{action_id}-p1",
                    replied_to,
                    *[created] * 3,
                ),
            )
            first_step = 2
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, local_step_number,
                short_step_id, step_type, step_name, status, goal_handle,
                retry_count, prompt_tokens, completion_tokens, user_message_id,
                user_message_json, user_request_text, accepted_sequence,
                adopted_process_id, started_at, completed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'user_request', 'user_request', 'success',
                      'S', 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{action_id}-s{first_step}",
                action_id,
                USER_ID,
                first_step,
                first_step,
                f"S-{first_step}-USER",
                message.message_id,
                serialize_action_user_message(message),
                render_action_user_request_text(message),
                first_step,
                f"{action_id}-p1",
                *[created] * 3,
            ),
        )


def _memory_run(
    db_path: Path, process_id: str, *, age: timedelta, completed: bool = True
) -> None:
    started = format_utc_iso(NOW - age)
    with (
        open_memory_catalog_connection(
            db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
        ) as connection,
        connection,
    ):
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, started_at, updated_at,
                heartbeat_at, next_event_seq
            ) VALUES (?, ?, 'memory', ?, ?, ?, ?, 1)
            """,
            (process_id, USER_ID, "success" if completed else "error", *[started] * 3),
        )
        if completed:
            append_memory_update_completed_in_connection(
                connection=connection,
                process_id=process_id,
                published_sources=(),
                created_at=started,
            )


def _load(db_path: Path) -> tuple[DueOutcome, ...]:
    with open_memory_catalog_connection(
        db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS
    ) as connection:
        return load_due_outcomes(
            connection, user_id=USER_ID, enqueued_at=format_utc_iso(NOW)
        )


def _due(db_path: Path) -> list[tuple[str, int]]:
    return [(item.item_id, item.checkpoint_days) for item in _load(db_path)]


def test_with_no_completed_run_the_window_reaches_back_45_days(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    # Its 30-day checkpoint fell 46 days ago.
    _deliver(db_path, "too-old", age=timedelta(days=76))
    _deliver(db_path, "a", age=timedelta(days=74))
    _deliver(db_path, "not-shown", age=timedelta(days=8), has_suggestion=0)
    _deliver(db_path, "b", age=timedelta(days=2))
    _deliver(db_path, "not-yet", age=timedelta(hours=20))

    assert _due(db_path) == [("a", 30), ("b", 1)]


def test_the_range_starts_at_the_last_completed_run_before_this_one(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    _memory_run(db_path, "process-done", age=timedelta(days=3))
    # A failed run showed nothing, and one enqueued after this run is not
    # before it: neither moves the start.
    _memory_run(db_path, "process-failed", age=timedelta(days=2), completed=False)
    _memory_run(db_path, "process-later", age=-timedelta(hours=1))
    # Its 1-day checkpoint fell 4 days ago, before the last completed run.
    _deliver(db_path, "seen", age=timedelta(days=5))
    # 2.5 days ago: after the completed run, before the failed one.
    _deliver(db_path, "after-done", age=timedelta(days=3, hours=12))
    _deliver(db_path, "seven", age=timedelta(days=9))

    assert _due(db_path) == [("after-done", 1), ("seven", 7)]


def test_at_most_five_are_due_the_earliest_checkpoint_first(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    for index, hours in enumerate((30, 60, 26, 40, 50, 28, 45)):
        _deliver(db_path, f"s-{index}", age=timedelta(hours=hours))

    assert _due(db_path) == [
        ("s-1", 1),
        ("s-4", 1),
        ("s-6", 1),
        ("s-3", 1),
        ("s-0", 1),
    ]


def test_an_action_is_due_from_its_first_completed_turn_with_what_was_asked(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    _deliver(
        db_path,
        "s-offer",
        age=timedelta(days=9),
        summary="## Why\nSlow reviews.\n### Hypothesis\nReviews close sooner.\n"
        "### Signals\nPR age.",
        contract="action_offer",
    )
    _deliver(db_path, "s-note", age=timedelta(days=9))
    _act(
        db_path,
        "taken-up",
        request="Draft the review checklist.",
        suggestion_id="s-offer",
        turns=(("completed", timedelta(days=8, hours=12)),),
    )
    _act(
        db_path,
        "replied",
        request="Who owns this?",
        replied_to="s-note",
        turns=(("completed", timedelta(days=8)),),
    )
    # Its first turn failed; the request was answered by the second, and a
    # later follow-up turn does not move its delivery.
    _act(
        db_path,
        "own",
        request="Summarize the contract.",
        turns=(
            ("failed", timedelta(days=9)),
            ("completed", timedelta(days=7, hours=12)),
            ("completed", timedelta(days=1)),
        ),
    )

    due = _load(db_path)

    assert [
        (item.kind, item.item_id, item.checkpoint_days, item.source_suggestion_id)
        for item in due
    ] == [
        ("suggestion", "s-note", 7, None),
        ("suggestion", "s-offer", 7, None),
        ("action", "taken-up", 7, "s-offer"),
        ("action", "replied", 7, "s-note"),
        ("action", "own", 7, None),
    ]
    expectations = {item.item_id: item.expectation for item in due}
    assert expectations["s-offer"] == "Reviews close sooner."
    assert expectations["s-note"] == ""
    assert expectations["own"] == "Summarize the contract."

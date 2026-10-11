"""Dismissed suggestions as a source of the unified Memory run."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.run_workspace import (
    MemoryRunWorkspaceScope,
)
from pantaray_agents.local_runtime.runtime.job_payload_models import (
    parse_memory_update_job_payload_json,
)
from pantaray_agents.local_runtime.runtime.memory_agent_dispatcher import (
    dispatch_memory_agent_triggers_once,
)
from pantaray_agents.local_runtime.runtime.memory_agent_triggers import (
    SHORT_INSIGHT_MEMORY_TRIGGER_KIND,
    insert_pending_memory_agent_trigger,
)
from pantaray_agents.local_runtime.runtime.memory_dismissal_range import (
    read_dismissed_suggestions,
)
from pantaray_agents.local_runtime.runtime.session_store import import_desktop_session
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.memory_file_editor import (
    LocalMemoryFileEditorRuntime,
)
from pantaray_agents.tasks.memory_update_context import prepare_memory_update_run

from .migrated_db import prepare_test_database

NOW = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def _db(tmp_path: Path) -> Path:
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


def _at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _dismiss(
    db_path: Path,
    suggestion_id: str,
    answer: str,
    *,
    minutes_ago: int,
    reason: str | None = None,
) -> None:
    at = _at(minutes_ago)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id, user_id, status, answer, prompt_name, prompt_version,
                has_suggestion, interaction_contract, user_reaction, rejected_at,
                action_status, dismiss_reason, created_at, updated_at
            ) VALUES (?, 'user-1', 'success', ?, 'suggestion', '1', 1,
                      'action_offer', 'rejected', ?, 'idle', ?, ?, ?)
            """,
            (suggestion_id, answer, at, reason, at, at),
        )


def _reply(db_path: Path, suggestion_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO processes(
                process_id, user_id, kind, status, action_id, next_event_seq,
                started_at, updated_at, heartbeat_at
            ) VALUES ('process-1', 'user-1', 'action', 'running', 'action-1', 1,
                      ?, ?, ?)
            """,
            (_at(0), _at(0), _at(0)),
        )
        connection.execute(
            """
            INSERT INTO agent_action_steps(
                step_id, action_id, user_id, step_number, local_step_number,
                short_step_id, step_type, step_name, status, goal_handle,
                retry_count, prompt_tokens, completion_tokens, llm_response_text,
                adopted_process_id, source_suggestion_id, started_at, completed_at,
                created_at
            ) VALUES (?, 'action-1', 'user-1', 1, 1, 'S-1-ASSISTANT',
                      'assistant_message', 'assistant_message', 'success', 'S',
                      0, 0, 0, 'offer', 'process-1', ?, ?, ?, ?)
            """,
            (f"step-{suggestion_id}", suggestion_id, _at(0), _at(0), _at(0)),
        )


def _dispatch(db_path: Path, now: datetime = NOW) -> None:
    dispatch_memory_agent_triggers_once(
        db_path=db_path, busy_timeout_ms=1_000, user_id="user-1", now=now
    )


def _handed(db_path: Path) -> list[list[str] | None]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT payloads.payload_json FROM jobs
            JOIN job_payloads AS payloads ON payloads.job_id = jobs.job_id
            WHERE jobs.job_type = 'memory_update' ORDER BY jobs.rowid
            """
        ).fetchall()
    return [json.loads(row[0]).get("dismissed_suggestion_ids") for row in rows]


def _finish_memory_jobs(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'completed' WHERE job_type = 'memory_update'"
        )


def _insight(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        insert_pending_memory_agent_trigger(
            connection=connection,
            user_id="user-1",
            trigger_kind=SHORT_INSIGHT_MEMORY_TRIGGER_KIND,
            source_id=f"insight-{uuid.uuid4().hex[:8]}",
            created_at=NOW.isoformat().replace("+00:00", "Z"),
        )


def test_a_recent_dismissal_alone_waits_for_the_insight_window(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    _dismiss(db_path, "s-1", "Shall I draft the notes?", minutes_ago=5)

    _dispatch(db_path)

    assert _handed(db_path) == []


def test_a_dismissal_older_than_the_window_starts_a_run_of_its_own(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    _dismiss(db_path, "s-1", "Shall I draft the notes?", minutes_ago=20)

    _dispatch(db_path)

    assert _handed(db_path) == [["s-1"]]


def test_dismissals_ride_along_once_and_the_cursor_moves_on(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _dismiss(db_path, "s-1", "Shall I draft the notes?", minutes_ago=2)
    _insight(db_path)

    _dispatch(db_path)
    _finish_memory_jobs(db_path)
    _dismiss(db_path, "s-2", "Shall I file the invoice?", minutes_ago=-30)
    _insight(db_path)
    _dispatch(db_path, NOW + timedelta(hours=1))
    _finish_memory_jobs(db_path)
    _insight(db_path)
    _dispatch(db_path, NOW + timedelta(hours=2))

    assert _handed(db_path) == [["s-1"], ["s-2"], None]


def test_a_dismissal_the_user_replied_to_is_not_handed(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _dismiss(db_path, "s-1", "Shall I draft the notes?", minutes_ago=20)
    _reply(db_path, "s-1")

    _dispatch(db_path)

    assert _handed(db_path) == []
    with sqlite3.connect(db_path) as connection:
        assert (
            read_dismissed_suggestions(
                connection, user_id="user-1", suggestion_ids=("s-1",)
            )
            == ()
        )


def test_the_next_run_reads_each_unanswered_dismissal_with_its_reason(
    tmp_path: Path,
) -> None:
    db_path = _db(tmp_path)
    _dismiss(
        db_path,
        "s-1",
        "Shall I draft the release notes?",
        minutes_ago=20,
        reason="I write those myself.",
    )
    _dismiss(db_path, "s-2", "Shall I file the invoice?", minutes_ago=10)
    _dismiss(db_path, "s-3", "Shall I book the room?", minutes_ago=5)
    _dispatch(db_path)
    # Answered after the run was queued, so its Action carries it instead.
    _reply(db_path, "s-3")

    with sqlite3.connect(db_path) as connection:
        payload = parse_memory_update_job_payload_json(
            connection.execute("SELECT payload_json FROM job_payloads").fetchone()[0]
        )
    runtime = LocalMemoryFileEditorRuntime(
        artifact_root=tmp_path / "artifacts", db_path=db_path, busy_timeout_ms=1_000
    )
    with MemoryRunWorkspaceScope() as scope:
        prepared = prepare_memory_update_run(
            runtime=runtime, payload=payload, workspace_scope=scope
        )

    assert payload.get("dismissed_suggestion_ids") == ["s-1", "s-2", "s-3"]
    first, second = prepared.context.dismissed_suggestions.split("\n\n### ")
    assert first.startswith("### suggestion s-1 (dismissed ")
    assert first.endswith(
        "Shall I draft the release notes?\n\n"
        "Dismissed, the user's reason: I write those myself."
    )
    assert second.startswith("suggestion s-2 (dismissed ")
    assert second.endswith("Shall I file the invoice?\n\nDismissed")
    assert prepared.has_evidence


def test_dismissals_recorded_before_the_cursor_existed_are_not_handed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path, 1_000, tuple(spec for spec in migrations if spec.version < 128)
    )
    import_desktop_session(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        desktop_access_token="header.payload.signature",
        expires_at="2099-08-16T00:00:00Z",
        session_version="1",
    )
    _dismiss(db_path, "s-old", "Shall I draft the notes?", minutes_ago=60)
    apply_migrations(db_path, 1_000, migrations)
    _dismiss(db_path, "s-new", "Shall I file the invoice?", minutes_ago=20)

    _dispatch(db_path)

    assert _handed(db_path) == [["s-new"]]

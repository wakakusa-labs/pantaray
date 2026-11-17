from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations

from .support import _insert_user, _migrations_before, apply_migrations

_COLUMNS = (
    "approval_session_id, user_id, action_id, tool_request_id, tool_id, "
    "intent_class, approval_source, status, approved_capabilities_json, "
    "command_summary_json, requested_at, decided_at, created_at"
)


def _insert_session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    source: str,
    status: str,
    decided_at: str | None,
    reason: str | None = None,
) -> None:
    columns = _COLUMNS + (", review_reason" if reason is not None else "")
    values = "?, " * (12 + (reason is not None)) + "?"
    connection.execute(
        f"INSERT INTO approval_sessions({columns}) VALUES ({values})",
        (
            session_id,
            "user-1",
            "action-1",
            f"request:{session_id}",
            "bash",
            "command",
            source,
            status,
            "{}",
            '{"command": "ls"}',
            "t0",
            decided_at,
            "t0",
            *((reason,) if reason is not None else ()),
        ),
    )


def test_stored_approvals_keep_their_decisions_and_reviews_need_a_reason(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    before = _migrations_before(migrations, "0143_approval_review_source.sql")
    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=before)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        _insert_user(connection, "user-1")
        connection.executescript(
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, "
            "created_at, updated_at) VALUES ('s-1', 'user-1', 'processing', 't', 't');"
            "INSERT INTO agent_actions(action_id, user_id, suggestion_id, "
            "initial_user_message_id, execution_target_json, status, final_output, "
            "prompt_name, prompt_version, created_at, updated_at) VALUES "
            "('action-1', 'user-1', 's-1', 'm-1', '{\"kind\":\"scratch\"}', "
            "'processing', '', 'p', 'v1', 't', 't');"
        )
        _insert_session(
            connection, "pending", source="prompt", status="pending", decided_at=None
        )
        _insert_session(
            connection,
            "allowed",
            source="settings",
            status="approved_once",
            decided_at="t1",
        )

    apply_migrations(db_path=db_path, busy_timeout_ms=1_000, migrations=migrations)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute(
            "SELECT approval_session_id, approval_source, status, decided_at, "
            "review_reason FROM approval_sessions ORDER BY approval_session_id"
        ).fetchall() == [
            ("allowed", "settings", "approved_once", "t1", None),
            ("pending", "prompt", "pending", None, None),
        ]
        _insert_session(
            connection,
            "reviewed",
            source="review",
            status="approved_once",
            decided_at="t2",
            reason="You asked for this export.",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_session(
                connection,
                "no-reason",
                source="review",
                status="approved_once",
                decided_at="t2",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_session(
                connection,
                "review-denied",
                source="review",
                status="denied",
                decided_at="t2",
                reason="Not asked for.",
            )
        # A request the review left to the user keeps its reason for the panel.
        _insert_session(
            connection,
            "asked",
            source="prompt",
            status="pending",
            decided_at=None,
            reason="You did not ask for this.",
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_session(
                connection,
                "settings-with-reason",
                source="settings",
                status="approved_once",
                decided_at="t2",
                reason="Stray reason.",
            )

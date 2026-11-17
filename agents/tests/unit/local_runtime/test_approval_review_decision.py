from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import load_default_migrations
from pantaray_agents.local_runtime.tooling.repository.approval_session_mutations import (
    record_review_approval_in_connection,
)

from .migrated_db import prepare_test_database


def _db_with_session(tmp_path: Path, *, status: str) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path, busy_timeout_ms=1_000, migrations=load_default_migrations()
    )
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "INSERT INTO users(user_id, ui_language, created_at, updated_at) "
            "VALUES ('user-1', 'ja', 't', 't');"
            "INSERT INTO agent_suggestions(suggestion_id, user_id, status, "
            "created_at, updated_at) VALUES ('s-1', 'user-1', 'processing', 't', 't');"
            "INSERT INTO agent_actions(action_id, user_id, suggestion_id, "
            "initial_user_message_id, execution_target_json, status, final_output, "
            "prompt_name, prompt_version, created_at, updated_at) VALUES "
            "('action-1', 'user-1', 's-1', 'm-1', '{\"kind\":\"scratch\"}', "
            "'processing', '', 'p', 'v1', 't', 't');"
        )
        connection.execute(
            "INSERT INTO approval_sessions(approval_session_id, user_id, action_id, "
            "tool_request_id, tool_id, intent_class, approval_source, status, "
            "approved_capabilities_json, command_summary_json, requested_at, "
            "decided_at, created_at) VALUES ('approval-1', 'user-1', 'action-1', "
            "'request-1', 'bash', 'command', 'prompt', ?, '{}', '{}', 't0', ?, 't0')",
            (status, None if status == "pending" else "t1"),
        )
    return db_path


def _record(db_path: Path) -> tuple[bool, tuple[object, ...]]:
    with sqlite3.connect(db_path) as connection:
        recorded = record_review_approval_in_connection(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            tool_request_id="request-1",
            approval_session_id="approval-1",
            reason="You asked for this export.",
            decided_at="t2",
        )
        row = connection.execute(
            "SELECT status, approval_source, review_reason, decided_at "
            "FROM approval_sessions WHERE approval_session_id = 'approval-1'"
        ).fetchone()
    return recorded, tuple(row)


def test_a_review_approval_stores_its_source_and_reason(tmp_path: Path) -> None:
    db_path = _db_with_session(tmp_path, status="pending")

    assert _record(db_path) == (
        True,
        ("approved_once", "review", "You asked for this export.", "t2"),
    )


@pytest.mark.parametrize("status", ["denied", "interrupted"])
def test_a_review_never_overrides_a_decision_made_while_it_ran(
    tmp_path: Path, status: str
) -> None:
    db_path = _db_with_session(tmp_path, status=status)

    assert _record(db_path) == (False, (status, "prompt", None, "t1"))

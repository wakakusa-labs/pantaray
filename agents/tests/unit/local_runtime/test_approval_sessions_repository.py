from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    MigrationError,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.brokering.approval_identity import (
    build_approval_preference_id,
    build_capability_grant_id,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    ApprovalSessionUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    apply_approval_decision_in_connection,
    create_capability_grant,
    interrupt_approval_session_for_tool_request,
    load_latest_approval_session,
    upsert_approval_preference,
    upsert_approval_session,
)

from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000


def test_upsert_approval_session_allows_missing_invocation_for_pending_request(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(status="pending"),
    )

    stored_session = load_latest_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        manifest_id="manifest:action-1",
        tool_request_id="request-1",
        tool_id="apply_patch",
    )

    assert stored_session is not None
    assert stored_session.approval_session_id == "approval-1"
    assert stored_session.tool_request_id == "request-1"
    assert stored_session.status == "pending"

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT tool_invocation_id
            FROM approval_sessions
            WHERE tool_request_id = ?
            """,
            ("request-1",),
        ).fetchone()

    assert row == (None,)


def test_upsert_approval_session_replay_preserves_initial_row(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(status="pending"),
    )
    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(
            approval_session_id="approval-2",
            status="approved_once",
            requested_at="2026-03-28T00:01:00Z",
            decided_at="2026-03-28T00:01:00Z",
        ),
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT approval_session_id, status, requested_at
            FROM approval_sessions
            WHERE tool_request_id = ?
            """,
            ("request-1",),
        ).fetchone()
        count_row = connection.execute(
            "SELECT COUNT(*) FROM approval_sessions WHERE tool_request_id = ?",
            ("request-1",),
        ).fetchone()

    assert row == ("approval-1", "pending", "2026-03-28T00:00:00Z")
    assert count_row == (1,)


def test_upsert_approval_session_rejects_identity_mismatch_for_existing_request(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(status="pending"),
    )

    with pytest.raises(MigrationError, match="identity mismatch"):
        upsert_approval_session(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            approval_session=_approval_session(
                approval_session_id="approval-2",
                tool_id="bash",
                intent_class="process_exec_local",
                command_summary_json={"summary": "pwd"},
            ),
        )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT approval_session_id, tool_id, command_summary_json
            FROM approval_sessions
            WHERE tool_request_id = ?
            """,
            ("request-1",),
        ).fetchone()

    assert row is not None
    assert row[0] == "approval-1"
    assert row[1] == "apply_patch"
    assert json.loads(row[2]) == {"summary": "patch todo.txt"}


def test_upsert_approval_session_rejects_capability_identity_mismatch_for_existing_request(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(
            status="pending",
            approved_capabilities_json={"required_capabilities": ["scoped_write"]},
        ),
    )

    with pytest.raises(MigrationError, match="identity mismatch"):
        upsert_approval_session(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            approval_session=_approval_session(
                approval_session_id="approval-2",
                approved_capabilities_json={
                    "required_capabilities": ["process_exec_local"]
                },
            ),
        )


def test_apply_approval_decision_conflict_leaves_preferences_and_grants_unchanged(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-prompt",
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="prompt_each_time",
            applies_to=("workspace_edit_and_command",),
            created_at="2026-03-28T00:00:00Z",
            updated_at="2026-03-28T00:00:00Z",
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-prompt",
            user_id="user-1",
            preference_id="pref-prompt",
            capability="scoped_write",
            scope_type="global",
            scope_ref=None,
            grant_source="prompt",
            granted_at="2026-03-28T00:00:01Z",
        ),
    )
    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(
            status="approved_once",
            approved_capabilities_json={"required_capabilities": ["scoped_write"]},
            decided_at="2026-03-28T00:00:02Z",
        ),
    )

    persisted_preference = ApprovalPreferenceUpsertInput(
        preference_id=build_approval_preference_id(
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            applies_to=("workspace_edit_and_command",),
        ),
        user_id="user-1",
        scope_type="global",
        scope_ref=None,
        approval_mode="always_allow",
        applies_to=("workspace_edit_and_command",),
        created_at="2026-03-28T00:00:03Z",
        updated_at="2026-03-28T00:00:03Z",
    )
    persisted_grant = CapabilityGrantCreateInput(
        grant_id=build_capability_grant_id(
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            capability="scoped_write",
            applies_to=("workspace_edit_and_command",),
        ),
        user_id="user-1",
        preference_id=persisted_preference.preference_id,
        capability="scoped_write",
        scope_type="global",
        scope_ref=None,
        grant_source="prompt",
        granted_at="2026-03-28T00:00:03Z",
    )

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        result = apply_approval_decision_in_connection(
            connection=connection,
            user_id="user-1",
            tool_request_id="request-1",
            action_id="action-1",
            approval_session_id="approval-1",
            expected_current_status="pending",
            next_status="approved_once",
            decided_at="2026-03-28T00:00:03Z",
            preference=persisted_preference,
            grants=(persisted_grant,),
        )
        preference_rows = connection.execute(
            """
            SELECT preference_id, approval_mode
            FROM approval_preferences
            WHERE user_id = ? AND scope_type = 'global' AND scope_ref IS NULL
            ORDER BY preference_id ASC
            """,
            ("user-1",),
        ).fetchall()
        grant_rows = connection.execute(
            """
            SELECT grant_id, preference_id, revoked_at
            FROM capability_grants
            WHERE user_id = ? AND scope_type = 'global' AND scope_ref IS NULL
            ORDER BY grant_id ASC
            """,
            ("user-1",),
        ).fetchall()

    assert result.status == "conflict"
    assert [tuple(row) for row in preference_rows] == [
        ("pref-prompt", "prompt_each_time")
    ]
    assert [tuple(row) for row in grant_rows] == [("grant-prompt", "pref-prompt", None)]


def test_interrupt_approval_session_marks_pending_request_interrupted(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(status="pending"),
    )

    updated = interrupt_approval_session_for_tool_request(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id="action-1",
        tool_request_id="request-1",
        interrupted_at="2026-03-28T00:00:05Z",
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT status, decided_at
            FROM approval_sessions
            WHERE tool_request_id = ?
            """,
            ("request-1",),
        ).fetchone()

    assert updated is True
    assert row == ("interrupted", "2026-03-28T00:00:05Z")


def test_interrupt_approval_session_does_not_update_decided_request(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    upsert_approval_session(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        approval_session=_approval_session(
            status="approved_once",
            decided_at="2026-03-28T00:00:02Z",
        ),
    )

    updated = interrupt_approval_session_for_tool_request(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id="action-1",
        tool_request_id="request-1",
        interrupted_at="2026-03-28T00:00:05Z",
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT status, decided_at
            FROM approval_sessions
            WHERE tool_request_id = ?
            """,
            ("request-1",),
        ).fetchone()

    assert updated is False
    assert row == ("approved_once", "2026-03-28T00:00:02Z")


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    _insert_agent_action(
        db_path=db_path,
        user_id="user-1",
        action_id="action-1",
        created_at="2026-03-28T00:00:00Z",
    )
    return db_path


def _insert_agent_action(
    *,
    db_path: Path,
    user_id: str,
    action_id: str,
    created_at: str,
) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_suggestions(
                suggestion_id,
                user_id,
                status,
                created_at,
                updated_at
            ) VALUES (?, ?, 'processing', ?, ?)
            """,
            (f"suggestion:{action_id}", user_id, created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id,
                user_id,
                suggestion_id,
                initial_user_message_id,
                execution_target_json,
                status,
                final_output,
                prompt_name,
                prompt_version,
                created_at,
                updated_at
            ) VALUES (
                ?, ?, ?, ?, '{"kind":"scratch"}',
                'processing', '', 'test/tooling', 'v1', ?, ?
            )
            """,
            (
                action_id,
                user_id,
                f"suggestion:{action_id}",
                f"message:{action_id}",
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO execution_sessions(
                execution_session_id,
                user_id,
                action_id,
                exec_mode,
                cwd_path,
                network_policy,
                capability_snapshot_json,
                status,
                started_at
            ) VALUES (?, ?, ?, 'brokered_file_ops', '.', 'cloud-proxy-only',
                      '{}', 'running', ?)
            """,
            (f"session:{action_id}", user_id, action_id, created_at),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO workspace_manifests(
                manifest_id,
                user_id,
                action_id,
                execution_session_id,
                scratch_root_path,
                created_at,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, 'ready')
            """,
            (
                f"manifest:{action_id}",
                user_id,
                action_id,
                f"session:{action_id}",
                "/tmp/scratch",
                created_at,
            ),
        )


def _approval_session(
    *,
    approval_session_id: str = "approval-1",
    tool_request_id: str = "request-1",
    user_id: str = "user-1",
    action_id: str = "action-1",
    manifest_id: str = "manifest:action-1",
    tool_invocation_id: str | None = None,
    tool_id: str = "apply_patch",
    intent_class: str = "surgical_edit",
    status: str = "pending",
    approved_capabilities_json: dict[str, object] | None = None,
    command_summary_json: dict[str, object] | None = None,
    requested_at: str = "2026-03-28T00:00:00Z",
    decided_at: str | None = None,
    claimed_at: str | None = None,
) -> ApprovalSessionUpsertInput:
    return ApprovalSessionUpsertInput(
        approval_session_id=approval_session_id,
        tool_request_id=tool_request_id,
        user_id=user_id,
        action_id=action_id,
        manifest_id=manifest_id,
        expected_execution_session_id=f"session:{action_id}",
        tool_invocation_id=tool_invocation_id,
        tool_id=tool_id,
        intent_class=intent_class,
        approval_source="prompt",
        status=status,
        approved_capabilities_json=approved_capabilities_json or {},
        command_summary_json=command_summary_json or {"summary": "patch todo.txt"},
        requested_at=requested_at,
        decided_at=decided_at,
        claimed_at=claimed_at,
    )

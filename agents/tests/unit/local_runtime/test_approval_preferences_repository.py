from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.approval_identity import (
    build_approval_preference_id,
    build_capability_grant_id,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    apply_approval_preference_setting,
    create_capability_grant,
    load_active_capability_grants,
    load_effective_approval_preference,
    upsert_approval_preference,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database

BUSY_TIMEOUT_MS = 1_000
APPLIES_TO_WORKSPACE_EDIT = ("workspace_edit_and_command",)


def test_approval_preference_and_capability_grant_are_persisted(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    insert_agent_action(
        db_path=db_path,
        created_at="2026-03-22T00:00:00Z",
    )
    ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-22T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-1",
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=APPLIES_TO_WORKSPACE_EDIT,
            created_at="2026-03-22T00:00:00Z",
            updated_at="2026-03-22T00:00:00Z",
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-1",
            user_id="user-1",
            preference_id="pref-1",
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at="2026-03-22T00:00:01Z",
        ),
    )

    with sqlite3.connect(db_path) as connection:
        preference_row = connection.execute(
            """
            SELECT approval_mode, applies_to_json
            FROM approval_preferences
            WHERE preference_id = ?
            """,
            ("pref-1",),
        ).fetchone()
        grant_row = connection.execute(
            """
            SELECT capability, scope_type, scope_ref
            FROM capability_grants
            WHERE grant_id = ?
            """,
            ("grant-1",),
        ).fetchone()

    assert preference_row == ("always_allow", '["workspace_edit_and_command"]')
    assert grant_row == ("process_exec_local", "global", None)


def test_apply_approval_preference_setting_revokes_duplicate_global_rows(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    canonical_preference_id = _workspace_edit_preference_id()
    for index, approval_mode in enumerate(
        ("prompt_each_time", "always_allow"), start=1
    ):
        upsert_approval_preference(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            preference=ApprovalPreferenceUpsertInput(
                preference_id=f"pref-legacy-{index}",
                user_id="user-1",
                scope_type="global",
                scope_ref=None,
                approval_mode=approval_mode,
                applies_to=APPLIES_TO_WORKSPACE_EDIT,
                created_at=f"2026-03-22T00:0{index}:00Z",
                updated_at=f"2026-03-22T00:0{index}:00Z",
            ),
        )

    apply_approval_preference_setting(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=_workspace_edit_preference(
            preference_id=canonical_preference_id,
            approval_mode="always_allow",
            timestamp="2026-03-22T00:03:00Z",
        ),
        grants=(
            _workspace_edit_grant(
                preference_id=canonical_preference_id,
                timestamp="2026-03-22T00:03:00Z",
            ),
        ),
        revoked_at="2026-03-22T00:03:00Z",
        revocation_reason="test cleanup",
    )

    with sqlite3.connect(db_path) as connection:
        active_preferences = connection.execute(
            """
            SELECT preference_id
            FROM approval_preferences
            WHERE user_id = ? AND scope_type = 'global' AND revoked_at IS NULL
            ORDER BY preference_id ASC
            """,
            ("user-1",),
        ).fetchall()
        active_grants = connection.execute(
            """
            SELECT grant_id
            FROM capability_grants
            WHERE user_id = ? AND scope_type = 'global' AND revoked_at IS NULL
            ORDER BY grant_id ASC
            """,
            ("user-1",),
        ).fetchall()

    assert active_preferences == [(canonical_preference_id,)]
    assert active_grants == [(_workspace_edit_grant_id(),)]


def test_apply_approval_preference_setting_preserves_other_global_domain(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    browser_preference_id = build_approval_preference_id(
        user_id="user-1",
        scope_type="global",
        scope_ref=None,
        applies_to=("browser_automation",),
    )
    edit_preference_id = _workspace_edit_preference_id()
    apply_approval_preference_setting(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=ApprovalPreferenceUpsertInput(
            preference_id=browser_preference_id,
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("browser_automation",),
            created_at="2026-03-22T00:00:00Z",
            updated_at="2026-03-22T00:00:00Z",
        ),
        grants=(
            CapabilityGrantCreateInput(
                grant_id=build_capability_grant_id(
                    user_id="user-1",
                    scope_type="global",
                    scope_ref=None,
                    capability="automation_control",
                    applies_to=("browser_automation",),
                ),
                user_id="user-1",
                preference_id=browser_preference_id,
                capability="automation_control",
                scope_type="global",
                scope_ref=None,
                grant_source="settings",
                granted_at="2026-03-22T00:00:00Z",
            ),
        ),
    )
    apply_approval_preference_setting(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=_workspace_edit_preference(
            preference_id=edit_preference_id,
            approval_mode="always_allow",
            timestamp="2026-03-22T00:01:00Z",
        ),
        revoked_at="2026-03-22T00:01:00Z",
        revocation_reason="test cleanup",
    )

    assert load_active_capability_grants(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        applies_to="browser_automation",
    ) == ("automation_control",)
    assert (
        load_active_capability_grants(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            applies_to="workspace_edit_and_command",
        )
        == ()
    )


def test_load_active_capability_grants_ignores_revoked_global_preference(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    preference_id = _workspace_edit_preference_id()
    apply_approval_preference_setting(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=_workspace_edit_preference(
            preference_id=preference_id,
            approval_mode="always_allow",
            timestamp="2026-03-22T00:00:00Z",
        ),
        grants=(
            _workspace_edit_grant(
                preference_id=preference_id,
                timestamp="2026-03-22T00:00:00Z",
            ),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE approval_preferences
            SET revoked_at = '2026-03-22T00:01:00Z'
            WHERE preference_id = ?
            """,
            (preference_id,),
        )

    assert (
        load_active_capability_grants(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            applies_to="workspace_edit_and_command",
        )
        == ()
    )


def test_load_active_capability_grants_ignores_unowned_global_grants(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)
    with sqlite3.connect(db_path) as connection:
        with connection:
            _insert_capability_grant(
                connection=connection,
                grant_id="null-pref-grant",
                preference_id=None,
            )
            _insert_capability_grant(
                connection=connection,
                grant_id="dangling-pref-grant",
                preference_id="missing-preference",
            )

    assert (
        load_active_capability_grants(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            applies_to="workspace_edit_and_command",
        )
        == ()
    )


def test_unsaved_default_auto_approves_only_workspace_edits_and_commands(
    tmp_path: Path,
) -> None:
    db_path = _bootstrap_db(tmp_path)

    workspace, screen = (
        load_effective_approval_preference(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            user_id="user-1",
            applies_to=applies_to,
        )
        for applies_to in ("workspace_edit_and_command", "screen_capture")
    )

    assert (workspace.preference_id, workspace.approval_mode) == (None, "always_allow")
    assert (screen.preference_id, screen.approval_mode) == (None, "prompt_each_time")


def test_a_saved_prompt_each_time_replaces_the_default(tmp_path: Path) -> None:
    db_path = _bootstrap_db(tmp_path)
    preference_id = _workspace_edit_preference_id()
    apply_approval_preference_setting(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        preference=_workspace_edit_preference(
            preference_id=preference_id,
            approval_mode="prompt_each_time",
            timestamp="2026-03-22T00:00:00Z",
        ),
    )

    preference = load_effective_approval_preference(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        user_id="user-1",
        applies_to="workspace_edit_and_command",
    )

    assert (preference.preference_id, preference.approval_mode) == (
        preference_id,
        "prompt_each_time",
    )


def _bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)
    return db_path


def _workspace_edit_preference_id() -> str:
    return build_approval_preference_id(
        user_id="user-1",
        scope_type="global",
        scope_ref=None,
        applies_to=APPLIES_TO_WORKSPACE_EDIT,
    )


def _workspace_edit_grant_id() -> str:
    return build_capability_grant_id(
        user_id="user-1",
        scope_type="global",
        scope_ref=None,
        capability="process_exec_local",
        applies_to=APPLIES_TO_WORKSPACE_EDIT,
    )


def _workspace_edit_preference(
    *,
    preference_id: str,
    approval_mode: str,
    timestamp: str,
) -> ApprovalPreferenceUpsertInput:
    return ApprovalPreferenceUpsertInput(
        preference_id=preference_id,
        user_id="user-1",
        scope_type="global",
        scope_ref=None,
        approval_mode=approval_mode,
        applies_to=APPLIES_TO_WORKSPACE_EDIT,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _workspace_edit_grant(
    *,
    preference_id: str,
    timestamp: str,
) -> CapabilityGrantCreateInput:
    return CapabilityGrantCreateInput(
        grant_id=_workspace_edit_grant_id(),
        user_id="user-1",
        preference_id=preference_id,
        capability="process_exec_local",
        scope_type="global",
        scope_ref=None,
        grant_source="settings",
        granted_at=timestamp,
    )


def _insert_capability_grant(
    *,
    connection: sqlite3.Connection,
    grant_id: str,
    preference_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO capability_grants(
            grant_id,
            user_id,
            preference_id,
            capability,
            scope_type,
            scope_ref,
            grant_source,
            granted_at,
            granted_by,
            revoked_at,
            revocation_reason
        ) VALUES (?, ?, ?, ?, 'global', NULL, 'settings', ?, 'user', NULL, NULL)
        """,
        (
            grant_id,
            "user-1",
            preference_id,
            "process_exec_local",
            "2026-03-22T00:00:01Z",
        ),
    )

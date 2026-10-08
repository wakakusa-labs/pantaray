from __future__ import annotations

import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.models import ToolInvocationStartInput
from pantaray_agents.local_runtime.tooling.repository import (
    record_tool_invocation_start,
)

from .support import (
    _configure_connection,
    _insert_user,
    _migrations_before,
    apply_migrations,
    load_default_migrations,
)

REMOVE_SYNTHETIC_TOOL_STEPS_MIGRATION_NAME = "0041_remove_synthetic_tool_steps.sql"
RETIRE_LEGACY_MEMORY_TRIGGERS_MIGRATION_NAME = "0077_retire_legacy_memory_triggers.sql"
ACTION_CREATION_CUTOVER_MIGRATION_NAME = "0082_action_creation_cutover.sql"


def test_retire_legacy_memory_triggers_cancels_active_post_action_jobs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations,
            RETIRE_LEGACY_MEMORY_TRIGGERS_MIGRATION_NAME,
        ),
    )
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_user(connection, "user-1")
            for suffix, status in (("active", "running"), ("done", "completed")):
                process_status = "running" if status == "running" else "completed"
                connection.execute(
                    """
                    INSERT INTO processes(
                        process_id, user_id, kind, status, started_at, updated_at,
                        completed_at, heartbeat_at, current_job_id, next_event_seq
                    ) VALUES (?, 'user-1', 'post_action_pipeline', ?, '2026-04-01T00:00:00Z',
                              '2026-04-01T00:00:00Z', ?, '2026-04-01T00:00:00Z', ?, 1)
                    """,
                    (
                        f"process-{suffix}",
                        process_status,
                        None if status == "running" else "2026-04-01T00:01:00Z",
                        f"job-{suffix}" if status == "running" else None,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, user_id, job_type, process_id, status, attempt,
                        claimed_by, claimed_at, heartbeat_at, scheduled_at,
                        started_at, completed_at, logical_key
                    ) VALUES (?, 'user-1', 'post_action_pipeline', ?, ?, 1, ?, ?, ?,
                              '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z', ?, ?)
                    """,
                    (
                        f"job-{suffix}",
                        f"process-{suffix}",
                        status,
                        "worker-1" if status == "running" else None,
                        "2026-04-01T00:00:00Z" if status == "running" else None,
                        "2026-04-01T00:00:30Z",
                        None if status == "running" else "2026-04-01T00:01:00Z",
                        f"action-{suffix}",
                    ),
                )
            connection.execute(
                """
                INSERT INTO job_attempts(
                    attempt_id, job_id, attempt_number, started_at, status
                ) VALUES (
                    'attempt-active', 'job-active', 1,
                    '2026-04-01T00:00:00Z', 'running'
                )
                """
            )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        active_job = connection.execute(
            "SELECT status, claimed_by, error_code FROM jobs WHERE job_id = 'job-active'"
        ).fetchone()
        completed_job = connection.execute(
            "SELECT status, error_code FROM jobs WHERE job_id = 'job-done'"
        ).fetchone()
        active_process = connection.execute(
            "SELECT status, current_job_id FROM processes WHERE process_id = 'process-active'"
        ).fetchone()
        active_attempt = connection.execute(
            "SELECT status, error_code FROM job_attempts WHERE attempt_id = 'attempt-active'"
        ).fetchone()
        summary_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(activity_summaries)")
        }
        summary_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(activity_summaries)")
        }

    assert active_job == ("canceled", None, "POST_ACTION_PIPELINE_RETIRED")
    assert completed_job == ("completed", None)
    assert active_process == ("canceled", None)
    assert active_attempt == ("canceled", "POST_ACTION_PIPELINE_RETIRED")
    assert "fact_followup_fact_id" not in summary_columns
    assert "fact_followup_enqueued_at" not in summary_columns
    assert "idx_activity_summaries_fact_followup_fact_id" not in summary_indexes


def test_remove_synthetic_tool_steps_migration_detaches_audit_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations,
            REMOVE_SYNTHETIC_TOOL_STEPS_MIGRATION_NAME,
        ),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_action_runtime_graph(connection)
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id,
                    action_id,
                    user_id,
                    step_number,
                    local_step_number,
                    short_step_id,
                    step_type,
                    step_name,
                    status,
                    created_at
                ) VALUES (
                    'synthetic-step-1',
                    'action-1',
                    'user-1',
                    15,
                    15,
                    'synthetic-15-TOOL',
                    'tool_execution',
                    'tool invocation',
                    'success',
                    '2026-03-22T00:00:01Z'
                )
                """
            )
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id="invoke-1",
            tool_request_id="tool-step-1",
            user_id="user-1",
            action_id="action-1",
            step_id="synthetic-step-1",
            tool_id="read",
            manifest_id="manifest:action-1",
            execution_session_id="execution-session-1",
            cwd=".",
            timeout_ms=5_000,
            intent_class="read_only",
            network_policy="cloud-proxy-only",
            command_summary_json=None,
            capability_snapshot_json={"allowed_capabilities": ["scoped_read"]},
            request_json={"path": "notes.txt"},
            status="running",
            started_at="2026-03-22T00:00:02Z",
        ),
    )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations,
            ACTION_CREATION_CUTOVER_MIGRATION_NAME,
        ),
    )

    with sqlite3.connect(db_path) as connection:
        synthetic_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM agent_action_steps
            WHERE short_step_id LIKE 'synthetic-%-TOOL'
            """
        ).fetchone()[0]
        invocation_row = connection.execute(
            """
            SELECT step_id, status
            FROM tool_invocations
            WHERE invocation_id = 'invoke-1'
            """
        ).fetchone()

    assert synthetic_count == 0
    assert invocation_row == (None, "running")


def test_approval_sessions_accept_interrupted_status_after_cutover(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_action_header(connection)
            connection.execute(
                """
                INSERT INTO approval_sessions(
                    approval_session_id,
                    user_id,
                    action_id,
                    manifest_id,
                    tool_request_id,
                    tool_id,
                    intent_class,
                    approval_source,
                    status,
                    approved_capabilities_json,
                    command_summary_json,
                    requested_at,
                    decided_at,
                    created_at
                ) VALUES (
                    'approval-1',
                    'user-1',
                    'action-1',
                    NULL,
                    'request-1',
                    'apply_patch',
                    'surgical_edit',
                    'prompt',
                    'interrupted',
                    '{}',
                    '{}',
                    '2026-03-22T00:00:00Z',
                    '2026-03-22T00:00:00Z',
                    '2026-03-22T00:00:00Z'
                )
                """
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM approval_sessions"
            ).fetchone()[0]

    assert count == 1


def _insert_action_runtime_graph(connection: sqlite3.Connection) -> None:
    _insert_action_header(connection)
    connection.execute(
        """
        INSERT INTO execution_sessions(
            execution_session_id,
            user_id,
            action_id,
            parent_execution_session_id,
            exec_mode,
            cwd_path,
            action_temp_dir,
            app_runtime_python,
            network_policy,
            capability_snapshot_json,
            tool_allowlist_json,
            status,
            started_at
        ) VALUES (
            'execution-session-1',
            'user-1',
            'action-1',
            NULL,
            'brokered_file_ops',
            '/tmp/workspace',
            '/tmp/workspace/.runtime-temp',
            '/usr/bin/python3',
            'cloud-proxy-only',
            '{"allowed_capabilities":["scoped_read"]}',
            '["read"]',
            'running',
            '2026-03-22T00:00:00Z'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO workspace_manifests(
            manifest_id,
            user_id,
            action_id,
            execution_session_id,
            virtual_root_path,
            scratch_root_path,
            created_at,
            materialized_at,
            status
        ) VALUES (
            'manifest:action-1',
            'user-1',
            'action-1',
            'execution-session-1',
            '/tmp/legacy-virtual-root',
            '/tmp/workspace',
            '2026-03-22T00:00:00Z',
            '2026-03-22T00:00:00Z',
            'ready'
        )
        """
    )


def _insert_action_header(connection: sqlite3.Connection) -> None:
    _insert_user(connection, "user-1")
    connection.execute(
        """
        INSERT INTO agent_suggestions(
            suggestion_id,
            user_id,
            status,
            answer,
            prompt_name,
            prompt_version,
            has_suggestion,
            interaction_contract,
            created_at,
            updated_at
        ) VALUES (
            'suggestion-1',
            'user-1',
            'success',
            'answer',
            'suggestion',
            '1.0',
            1,
            'action_offer',
            '2026-03-22T00:00:00Z',
            '2026-03-22T00:00:00Z'
        )
        """
    )
    action_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(agent_actions)")
    }
    if "initial_user_message_id" in action_columns:
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id, user_id, suggestion_id, initial_user_message_id,
                status, execution_target_json, final_output, prompt_name,
                prompt_version, created_at, updated_at
            ) VALUES (
                'action-1', 'user-1', 'suggestion-1', 'message-action-1',
                'processing', '{"kind":"scratch"}', '', 'action/executing',
                '1.0', '2026-03-22T00:00:00Z', '2026-03-22T00:00:00Z'
            )
            """
        )
    else:
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id, user_id, suggestion_id, status, final_output,
                prompt_name, prompt_version, created_at, updated_at
            ) VALUES (
                'action-1', 'user-1', 'suggestion-1', 'processing', '',
                'action/executing', '1.0', '2026-03-22T00:00:00Z',
                '2026-03-22T00:00:00Z'
            )
            """
        )

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
    _migrations_through,
    apply_migrations,
    load_default_migrations,
)

REMOVE_SYNTHETIC_TOOL_STEPS_MIGRATION_NAME = "0041_remove_synthetic_tool_steps.sql"
ACTION_CREATION_CUTOVER_MIGRATION_NAME = "0082_action_creation_cutover.sql"


def _insert_pre_cutover_action_runtime(connection: sqlite3.Connection) -> None:
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
    connection.execute(
        """
        INSERT INTO agent_actions(
            action_id,
            user_id,
            suggestion_id,
            status,
            final_output,
            prompt_name,
            prompt_version,
            created_at,
            updated_at
        ) VALUES (
            'action-1',
            'user-1',
            'suggestion-1',
            'processing',
            '',
            'action/executing',
            '1.0',
            '2026-03-22T00:00:00Z',
            '2026-03-22T00:00:00Z'
        )
        """
    )
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


def test_apply_migrations_creates_core_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert "schema_versions" in tables
    assert "migration_journal" in tables
    assert "runtime_recovery_runs" in tables
    assert "job_payloads" in tables
    assert "processes" in tables
    assert "jobs" in tables
    assert "agent_suggestions" in tables
    assert "agent_actions" in tables
    assert "agent_action_steps" in tables
    assert "agent_process_events" in tables
    assert "agent_suggestion_history" in tables
    assert "activity_logs" in tables
    assert "activity_summaries" in tables
    assert "agent_suggestion_critics" in tables
    assert "action_desires" in tables
    assert "action_check_items" in tables
    assert "action_goals" in tables
    assert "action_requirements" in tables
    assert "action_completed_goals" in tables
    assert "allowed_roots" not in tables
    assert "workspaces" not in tables
    assert "execution_sessions" in tables
    assert "tool_definitions" in tables
    assert "tool_invocations" in tables
    assert "tool_outputs" in tables
    assert "tool_redactions" in tables
    assert "agent_insights" in tables
    assert "agent_insight_update_runs" in tables
    assert "agent_long_term_insight_state" in tables
    assert "agent_facts" in tables
    assert "agent_fact_structuring_runs" in tables
    assert "memory_artifacts" in tables
    assert "memory_artifact_files" in tables
    assert "memory_artifact_blocks" in tables
    assert "memory_artifact_blocks_fts" in tables
    assert "scheduler_jobs" in tables
    assert "scheduler_job_runs" in tables
    assert "capability_grants" in tables
    assert "approval_preferences" in tables
    assert "approval_sessions" in tables
    assert "artifact_manifest" not in tables
    assert "file_references" in tables
    assert "retention_jobs" in tables
    assert "fts_jobs" in tables
    assert "import_runs" in tables
    assert "import_run_sources" in tables
    assert "feature_readiness" not in tables

    with sqlite3.connect(db_path) as conn:
        activity_log_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(activity_logs)")
        }
        activity_summary_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(activity_summaries)")
        }

    assert "prompt_text" in activity_log_columns
    assert "response_text" not in activity_log_columns
    assert "prompt_text" in activity_summary_columns
    assert "response_text" not in activity_summary_columns
    assert "fact_followup_fact_id" not in activity_summary_columns
    assert "fact_followup_enqueued_at" not in activity_summary_columns


def test_activity_response_text_migration_removes_duplicate_columns_and_keeps_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations, "0045_drop_activity_response_text.sql"
        ),
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO activity_logs(
                    log_id,
                    user_id,
                    period_start,
                    period_end,
                    description,
                    status,
                    error,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    source_capture_paths,
                    used_image_count,
                    created_at,
                    updated_at
                ) VALUES (
                    'log-1',
                    'user-1',
                    '2026-03-27T00:00:00Z',
                    '2026-03-27T00:04:00Z',
                    'legacy activity description',
                    'success',
                    NULL,
                    'activity prompt',
                    'duplicated response',
                    'activity_description',
                    '1.0',
                    '[]',
                    0,
                    '2026-03-27T00:00:00Z',
                    '2026-03-27T00:04:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO activity_summaries(
                    summary_id,
                    user_id,
                    summary_type,
                    period_start,
                    period_end,
                    summary,
                    status,
                    error,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    source_ids,
                    created_at,
                    updated_at,
                    fact_followup_fact_id,
                    fact_followup_enqueued_at
                ) VALUES (
                    'summary-1',
                    'user-1',
                    '24h',
                    '2026-03-26T00:00:00Z',
                    '2026-03-27T00:00:00Z',
                    'legacy activity summary',
                    'success',
                    NULL,
                    'summary prompt',
                    'duplicated summary response',
                    'activity_summary',
                    '1.0',
                    '["log-1"]',
                    '2026-03-27T00:00:00Z',
                    '2026-03-27T00:04:00Z',
                    'fact-1',
                    '2026-03-27T00:05:00Z'
                )
                """
            )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        log_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(activity_logs)")
        }
        summary_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(activity_summaries)")
        }
        log_row = connection.execute(
            """
            SELECT description, prompt_text
            FROM activity_logs
            WHERE log_id = 'log-1'
            """
        ).fetchone()
        summary_row = connection.execute(
            """
            SELECT summary, prompt_text
            FROM activity_summaries
            WHERE summary_id = 'summary-1'
            """
        ).fetchone()
        log_fts_row = connection.execute(
            """
            SELECT rowid
            FROM memory_search_activity_logs_fts
            WHERE memory_search_activity_logs_fts MATCH 'legacy'
            """
        ).fetchone()
        summary_fts_row = connection.execute(
            """
            SELECT rowid
            FROM memory_search_activity_summaries_fts
            WHERE memory_search_activity_summaries_fts MATCH 'legacy'
            """
        ).fetchone()

    assert "response_text" not in log_columns
    assert "response_text" not in summary_columns
    assert log_row == ("legacy activity description", "activity prompt")
    assert summary_row == ("legacy activity summary", "summary prompt")
    assert log_fts_row is not None
    assert summary_fts_row is not None


def test_latest_schema_uses_local_paths_instead_of_virtual_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as connection:
        workspace_folder_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workspace_folders)")
        }
        workspace_manifest_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(workspace_manifests)")
        }
        workspace_root_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(workspace_manifest_roots)")
        }
        file_reference_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(file_references)")
        }

    assert "virtual_slug" not in workspace_folder_columns
    assert "virtual_root_path" not in workspace_manifest_columns
    assert "workspace_manifest_mounts" not in _table_names(db_path)
    assert "virtual_name" not in workspace_root_columns
    assert "virtual_path" not in workspace_root_columns
    assert {"real_path", "canonical_real_path"} <= workspace_root_columns
    assert "virtual_path" not in file_reference_columns
    assert {"local_path", "canonical_local_path"} <= file_reference_columns


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_long_term_state_backfill_keeps_legacy_pointer_when_insight_row_is_error(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    migration_40 = next(
        migration
        for migration in migrations
        if migration.name == "0040_agent_insight_updates.sql"
    )
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(migrations, "0040_agent_insight_updates.sql"),
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
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
                    'prompt',
                    'response',
                    'prompt',
                    'v1',
                    1,
                    'action_offer',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_insights(
                    insight_id,
                    user_id,
                    suggestion_id,
                    status,
                    short_term_insight_data,
                    facts,
                    error,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    insight_profile_brief,
                    long_term_insight_storage_path,
                    long_term_insight_sha256,
                    created_at,
                    updated_at
                ) VALUES (
                    'insight-1',
                    'user-1',
                    'suggestion-1',
                    'error',
                    'short term',
                    'facts',
                    '{"error_code":"INSIGHT_UPD_SYSTEM_ERROR"}',
                    'prompt',
                    'response',
                    'insight',
                    '1.0',
                    'legacy brief',
                    'users/user-1/insights/long_term.md',
                    'sha-legacy',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:10:00Z'
                )
                """
            )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_through(migrations, "0040_agent_insight_updates.sql"),
    )

    with sqlite3.connect(db_path) as connection:
        state_row = connection.execute(
            """
            SELECT user_id, source_insight_id, storage_path, sha256, insight_profile_brief
            FROM agent_long_term_insight_state
            WHERE user_id = 'user-1'
            """
        ).fetchone()
        run_row = connection.execute(
            """
            SELECT status, final_storage_path
            FROM agent_insight_update_runs
            WHERE insight_update_id = 'legacy-insight-1'
            """
        ).fetchone()

    assert state_row == (
        "user-1",
        "insight-1",
        "users/user-1/insights/long_term.md",
        "sha-legacy",
        "legacy brief",
    )
    assert run_row == ("success", "users/user-1/insights/long_term.md")

    with sqlite3.connect(db_path) as conn:
        version_row = conn.execute(
            """
            SELECT current_version
            FROM schema_versions
            WHERE component = 'local_runtime'
            """
        ).fetchone()
    assert version_row is not None
    assert int(version_row[0]) == migration_40.version

    with sqlite3.connect(db_path) as conn:
        event_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_process_events)")
        }

        history_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(agent_suggestion_history)")
        }
        jobs_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        activity_summary_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(activity_summaries)")
        }
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        scheduler_job_run_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(scheduler_job_runs)")
        }
        action_step_indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(agent_action_steps)")
        }
        action_steps_ddl_row = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'agent_action_steps'
            """
        ).fetchone()

    assert {
        "event_id",
        "suggestion_id",
        "user_id",
        "action_id",
        "sequence",
        "payload",
    } <= event_columns
    assert {
        "suggestion_id",
        "suggestion_created_at",
        "suggestion_updated_at",
        "suggestion_status",
        "has_suggestion",
        "action_request_payload_present",
    } <= history_columns
    assert "logical_key" in jobs_columns
    assert "fact_followup_fact_id" in activity_summary_columns
    assert "fact_followup_enqueued_at" in activity_summary_columns
    assert "idx_scheduler_job_runs_unique_window" in scheduler_job_run_indexes
    assert "idx_agent_action_steps_short_step_id_resolution" in action_step_indexes
    assert "session_state" not in user_columns
    assert "desktop_access_token" not in user_columns
    assert "desktop_access_token_expires_at" not in user_columns
    assert "session_version" not in user_columns
    assert "token_imported_at" not in user_columns
    assert action_steps_ddl_row is not None
    action_steps_ddl = str(action_steps_ddl_row[0])
    assert "UNIQUE (action_id, step_number)" not in action_steps_ddl
    assert "UNIQUE (action_id, short_step_id)" not in action_steps_ddl


def test_long_term_state_backfill_uses_latest_valid_legacy_pointer(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(migrations, "0040_agent_insight_updates.sql"),
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
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
                    'prompt',
                    'response',
                    'prompt',
                    'v1',
                    1,
                    'action_offer',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            for insight_id, path, sha, updated_at in (
                (
                    "insight-old",
                    "users/user-1/insights/old.md",
                    "sha-old",
                    "2026-03-24T00:10:00Z",
                ),
                (
                    "insight-new",
                    "users/user-1/insights/new.md",
                    "sha-new",
                    "2026-03-24T00:20:00Z",
                ),
            ):
                connection.execute(
                    """
                    INSERT INTO agent_insights(
                        insight_id,
                        user_id,
                        suggestion_id,
                        status,
                        short_term_insight_data,
                        facts,
                        prompt_text,
                        response_text,
                        prompt_name,
                        prompt_version,
                        insight_profile_brief,
                        long_term_insight_storage_path,
                        long_term_insight_sha256,
                        created_at,
                        updated_at
                    ) VALUES (?, 'user-1', 'suggestion-1', 'success', 'short', 'facts',
                              'prompt', 'response', 'insight', '1.0', ?, ?, ?,
                              '2026-03-24T00:00:00Z', ?)
                    """,
                    (insight_id, f"brief-{insight_id}", path, sha, updated_at),
                )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_through(migrations, "0040_agent_insight_updates.sql"),
    )

    with sqlite3.connect(db_path) as connection:
        state_row = connection.execute(
            """
            SELECT source_insight_id, storage_path, sha256
            FROM agent_long_term_insight_state
            WHERE user_id = 'user-1'
            """
        ).fetchone()

    assert state_row == ("insight-new", "users/user-1/insights/new.md", "sha-new")


def test_fact_structuring_runs_are_independent_append_only_run_logs(
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
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_fact_structuring_runs(
                    fact_run_id,
                    user_id,
                    fact_id,
                    status,
                    prompt_name,
                    prompt_version,
                    created_at,
                    updated_at
                ) VALUES (
                    'run-1',
                    'user-1',
                    'fact-1',
                    'error',
                    'fact_structuring',
                    '1.0',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_fact_structuring_runs(
                    fact_run_id,
                    user_id,
                    fact_id,
                    status,
                    prompt_name,
                    prompt_version,
                    created_at,
                    updated_at
                ) VALUES (
                    'run-2',
                    'user-1',
                    'fact-1',
                    'success',
                    'fact_structuring',
                    '1.0',
                    '2026-03-24T00:10:00Z',
                    '2026-03-24T00:10:00Z'
                )
                """
            )
            count = connection.execute(
                """
                SELECT COUNT(*)
                FROM agent_fact_structuring_runs
                WHERE fact_id = 'fact-1'
                """
            ).fetchone()

    assert count == (2,)


def test_long_term_current_state_survives_source_insight_deletion(
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
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
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
                    'prompt',
                    'response',
                    'prompt',
                    'v1',
                    1,
                    'action_offer',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_insights(
                    insight_id,
                    user_id,
                    suggestion_id,
                    status,
                    short_term_insight_data,
                    facts,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    created_at,
                    updated_at
                ) VALUES (
                    'source-insight-1',
                    'user-1',
                    'suggestion-1',
                    'success',
                    'short',
                    'facts',
                    'prompt',
                    'response',
                    'insight',
                    '1.0',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_long_term_insight_state(
                    user_id,
                    source_insight_id,
                    source_run_id,
                    storage_path,
                    sha256,
                    insight_profile_brief,
                    created_at,
                    updated_at
                ) VALUES (
                    'user-1',
                    'source-insight-1',
                    'run-1',
                    'users/user-1/insights/long_term.md',
                    'sha-1',
                    'long-term brief',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                "DELETE FROM agent_insights WHERE insight_id = 'source-insight-1'"
            )
            state_row = connection.execute(
                """
                SELECT source_insight_id, storage_path, insight_profile_brief
                FROM agent_long_term_insight_state
                WHERE user_id = 'user-1'
                """
            ).fetchone()

    assert state_row == (
        "source-insight-1",
        "users/user-1/insights/long_term.md",
        "long-term brief",
    )


def test_agent_run_logs_do_not_depend_on_current_state_tables(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as connection:
        insight_update_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(agent_insight_update_runs)"
            )
        }
        insight_update_step_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(agent_insight_update_run_steps)"
            )
        }
        insight_update_foreign_tables = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(agent_insight_update_runs)"
            )
        }
        insight_update_step_foreign_tables = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(agent_insight_update_run_steps)"
            )
        }
        fact_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(agent_fact_structuring_runs)"
            )
        }
        fact_step_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(agent_fact_structuring_run_steps)"
            )
        }
        fact_foreign_tables = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(agent_fact_structuring_runs)"
            )
        }
        fact_step_foreign_tables = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(agent_fact_structuring_run_steps)"
            )
        }
        long_term_state_foreign_tables = {
            row[2]
            for row in connection.execute(
                "PRAGMA foreign_key_list(agent_long_term_insight_state)"
            )
        }
        long_term_state_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(agent_long_term_insight_state)"
            )
        }

    assert insight_update_columns["insight_id"][3] == 0
    assert long_term_state_columns["source_insight_id"][3] == 0
    assert {"prompt_text", "response_text", "patch_text"}.isdisjoint(
        insight_update_columns
    )
    assert {
        "llm_prompt_text",
        "llm_response_text",
        "tool_name",
        "tool_input_json",
        "tool_output_json",
        "error_code",
        "error_message",
    } <= set(insight_update_step_columns)
    assert {"prompt_text", "response_text", "patch_text"}.isdisjoint(
        insight_update_step_columns
    )
    assert {"prompt_text", "response_text", "patch_text"}.isdisjoint(fact_columns)
    assert {
        "llm_prompt_text",
        "llm_response_text",
        "tool_name",
        "tool_input_json",
        "tool_output_json",
        "error_code",
        "error_message",
    } <= set(fact_step_columns)
    assert {"prompt_text", "response_text", "patch_text"}.isdisjoint(fact_step_columns)
    assert "agent_insights" not in insight_update_foreign_tables
    assert insight_update_step_foreign_tables == {"agent_insight_update_runs"}
    assert "agent_facts" not in fact_foreign_tables
    assert fact_step_foreign_tables == {"agent_fact_structuring_runs"}
    assert "agent_insights" not in long_term_state_foreign_tables


def test_long_term_state_logical_source_migration_removes_legacy_insight_fk(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(
            migrations,
            "0043_long_term_state_logical_source.sql",
        ),
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
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
                    'prompt',
                    'response',
                    'prompt',
                    'v1',
                    1,
                    'action_offer',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_insights(
                    insight_id,
                    user_id,
                    suggestion_id,
                    status,
                    short_term_insight_data,
                    facts,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    created_at,
                    updated_at
                ) VALUES (
                    'source-insight-1',
                    'user-1',
                    'suggestion-1',
                    'success',
                    'short',
                    'facts',
                    'prompt',
                    'response',
                    'insight',
                    '1.0',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute("DROP TABLE agent_long_term_insight_state")
            connection.execute(
                """
                CREATE TABLE agent_long_term_insight_state (
                    user_id TEXT PRIMARY KEY,
                    source_insight_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    insight_profile_brief TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                    FOREIGN KEY (source_insight_id) REFERENCES agent_insights(insight_id) ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_long_term_insight_state(
                    user_id,
                    source_insight_id,
                    source_run_id,
                    storage_path,
                    sha256,
                    insight_profile_brief,
                    created_at,
                    updated_at
                ) VALUES (
                    'user-1',
                    'source-insight-1',
                    'run-1',
                    'users/user-1/insights/long_term.md',
                    'sha-1',
                    'long-term brief',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            connection.execute(
                "DELETE FROM agent_insights WHERE insight_id = 'source-insight-1'"
            )
            state_row = connection.execute(
                """
                SELECT source_insight_id, storage_path, insight_profile_brief
                FROM agent_long_term_insight_state
                WHERE user_id = 'user-1'
                """
            ).fetchone()
            foreign_tables = {
                row[2]
                for row in connection.execute(
                    "PRAGMA foreign_key_list(agent_long_term_insight_state)"
                )
            }

    assert state_row == (
        "source-insight-1",
        "users/user-1/insights/long_term.md",
        "long-term brief",
    )
    assert "agent_insights" not in foreign_tables


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
            _insert_pre_cutover_action_runtime(connection)
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


def test_agent_memory_run_logs_migration_replays_stale_version_40_checksum(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()
    migration_40 = next(
        migration
        for migration in migrations
        if migration.name == "0040_agent_insight_updates.sql"
    )
    latest_migration = migrations[-1]
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=_migrations_before(migrations, "0040_agent_insight_updates.sql"),
    )

    with sqlite3.connect(db_path) as connection:
        _configure_connection(connection, busy_timeout_ms=1_000)
        with connection:
            connection.execute(
                """
                UPDATE schema_versions
                SET current_version = ?,
                    migration_name = ?,
                    checksum = ?
                WHERE component = 'local_runtime'
                """,
                (migration_40.version, migration_40.name, "stale-version-40-checksum"),
            )

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        checksum_row = connection.execute(
            """
            SELECT current_version, checksum
            FROM schema_versions
            WHERE component = 'local_runtime'
            """
        ).fetchone()

    assert "agent_insight_update_runs" in tables
    assert "agent_long_term_insight_state" in tables
    assert "agent_fact_structuring_runs" in tables
    assert checksum_row == (
        latest_migration.version,
        latest_migration.checksum_sha256,
    )


def test_apply_migrations_keeps_suggestion_reaction_as_text_domain(
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
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
                    prompt_name,
                    prompt_version,
                    has_suggestion,
                    interaction_contract,
                    user_reaction,
                    created_at,
                    updated_at
                ) VALUES (
                    'suggestion-1',
                    'user-1',
                    'success',
                    'answer',
                    'prompt',
                    'response',
                    'prompt',
                    'v1',
                    1,
                    'action_offer',
                    'ignored',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_suggestion_history(
                    suggestion_id,
                    user_id,
                    suggestion_created_at,
                    suggestion_updated_at,
                    suggestion_status,
                    has_suggestion,
                    answer,
                    interaction_contract,
                    user_reaction,
                    action_request_payload_present,
                    last_sequence
                ) VALUES (
                    'suggestion-1',
                    'user-1',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z',
                    'success',
                    1,
                    'answer',
                    'action_offer',
                    'legacy_future_value',
                    0,
                    1
                )
                """
            )
        suggestion_reaction = connection.execute(
            """
            SELECT user_reaction
            FROM agent_suggestions
            WHERE suggestion_id = 'suggestion-1'
            """
        ).fetchone()
        history_reaction = connection.execute(
            """
            SELECT user_reaction
            FROM agent_suggestion_history
            WHERE suggestion_id = 'suggestion-1'
            """
        ).fetchone()

    assert suggestion_reaction == ("ignored",)
    assert history_reaction == ("legacy_future_value",)


def test_apply_migrations_allows_duplicate_action_step_resolution_keys(
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
            _insert_user(connection, "user-1")
            connection.execute(
                """
                INSERT INTO agent_suggestions(
                    suggestion_id,
                    user_id,
                    status,
                    answer,
                    prompt_text,
                    response_text,
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
                    'prompt',
                    'response',
                    'prompt',
                    'v1',
                    1,
                    'action_offer',
                    '2026-03-24T00:00:00Z',
                    '2026-03-24T00:00:00Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id,
                    user_id,
                    suggestion_id,
                    initial_user_message_id,
                    status,
                    execution_target_json,
                    final_output,
                    prompt_name,
                    prompt_version,
                    created_at,
                    updated_at
                ) VALUES (
                    'action-1',
                    'user-1',
                    'suggestion-1',
                    'message-action-1',
                    'processing',
                    '{"kind":"scratch"}',
                    '',
                    'action/executing',
                    '1.0',
                    '2026-03-24T00:00:01Z',
                    '2026-03-24T00:00:01Z'
                )
                """
            )
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
                    goal_handle,
                    retry_count,
                    prompt_tokens,
                    completion_tokens,
                    created_at
                ) VALUES (
                    'step-1',
                    'action-1',
                    'user-1',
                    1,
                    1,
                    'S-1-THINK',
                    'llm_output',
                    'supervisor_think',
                    'success',
                    'S',
                    0,
                    0,
                    0,
                    '2026-03-24T00:00:02Z'
                )
                """
            )
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
                    goal_handle,
                    retry_count,
                    prompt_tokens,
                    completion_tokens,
                    created_at
                ) VALUES (
                    'step-2',
                    'action-1',
                    'user-1',
                    1,
                    1,
                    'S-1-THINK',
                    'llm_output',
                    'supervisor_think',
                    'error',
                    'S',
                    1,
                    0,
                    0,
                    '2026-03-24T00:00:03Z'
                )
                """
            )

        count_row = connection.execute(
            """
            SELECT COUNT(*)
            FROM agent_action_steps
            WHERE action_id = 'action-1'
              AND step_number = 1
              AND short_step_id = 'S-1-THINK'
            """
        ).fetchone()

    assert count_row is not None
    assert int(count_row[0]) == 2


def test_apply_migrations_genericizes_process_status_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.db"

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )

    with sqlite3.connect(db_path) as connection:
        ddl_row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'processes'
            """
        ).fetchone()

    assert ddl_row is not None
    ddl = str(ddl_row[0])
    assert "'success'" in ddl
    assert "'error'" in ddl
    assert "'abandoned'" in ddl


def test_apply_migrations_enforces_message_only_action_invariant(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    migrations = load_default_migrations()

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations[:25],
    )

    with sqlite3.connect(db_path) as connection:
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
                'message only suggestion',
                'suggestion',
                '1.0',
                1,
                'message_only',
                '2026-03-23T00:00:00Z',
                '2026-03-23T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_suggestion_history(
                suggestion_id,
                user_id,
                suggestion_created_at,
                suggestion_updated_at,
                suggestion_status,
                has_suggestion,
                answer,
                interaction_contract,
                user_reaction,
                accepted_at,
                rejected_at,
                action_status,
                action_failure_code,
                action_failure_stage,
                action_failure_message_public,
                action_request_payload_present,
                action_id,
                action_created_at,
                action_updated_at,
                final_output,
                last_sequence
            ) VALUES (
                'suggestion-1',
                'user-1',
                '2026-03-23T00:00:00Z',
                '2026-03-23T00:00:00Z',
                'success',
                1,
                'message only suggestion',
                'message_only',
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                NULL,
                0,
                NULL,
                NULL,
                NULL,
                NULL,
                1
            )
            """
        )
        connection.commit()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO agent_actions(
                action_id,
                user_id,
                suggestion_id,
                status,
                final_output,
                prompt_name,
                prompt_version,
                created_at,
                updated_at
            ) VALUES (
                'action-1',
                'user-1',
                'suggestion-1',
                'processing',
                '',
                'suggestion',
                '1.0',
                '2026-03-23T00:00:00Z',
                '2026-03-23T00:00:00Z'
            )
            """
        )
        connection.commit()

    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=migrations,
    )

    with sqlite3.connect(db_path) as connection:
        action_row = connection.execute(
            """
            SELECT 1
            FROM agent_actions
            WHERE suggestion_id = 'suggestion-1'
            """
        ).fetchone()
        assert action_row is None

        with pytest.raises(
            sqlite3.IntegrityError,
            match="message_only suggestions must not carry action lane state",
        ):
            connection.execute(
                """
                UPDATE agent_suggestions
                SET action_status = 'processing'
                WHERE suggestion_id = 'suggestion-1'
                """
            )
        connection.rollback()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="message_only history rows must not carry action lane state",
        ):
            connection.execute(
                """
                UPDATE agent_suggestion_history
                SET action_status = 'processing'
                WHERE suggestion_id = 'suggestion-1'
                """
            )
        connection.rollback()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="message_only suggestions must not own action rows",
        ):
            connection.execute(
                """
                INSERT INTO agent_actions(
                    action_id,
                    user_id,
                    suggestion_id,
                    status,
                    final_output,
                    prompt_name,
                    prompt_version,
                    created_at,
                    updated_at
                ) VALUES (
                    'action-2',
                    'user-1',
                    'suggestion-1',
                    'processing',
                    '',
                    'suggestion',
                    '1.0',
                    '2026-03-23T00:00:00Z',
                    '2026-03-23T00:00:00Z'
                )
                """
            )

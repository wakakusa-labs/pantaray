from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from pantaray_agents import __version__ as APP_VERSION

from ..transactions import immediate_transaction
from .action_assistant_messages import apply_action_assistant_messages_migration
from .action_creation_cutover import apply_action_creation_cutover_migration
from .action_file_read_memory import apply_action_file_read_memory_migration
from .action_history_alignment import apply_local_action_state_migration
from .action_job_payload_enqueued_at import (
    apply_action_job_payload_enqueued_at_migration,
)
from .action_status_alignment import apply_action_status_alignment_migration
from .action_subagent_processes import apply_action_subagent_processes_migration
from .action_user_process_ownership import apply_action_user_process_ownership_migration
from .action_user_state_contract import apply_action_user_state_contract_migration
from .activity_summary_fact_followup import (
    apply_activity_summary_fact_followup_migration,
)
from .command_invocation_executable_sources import (
    apply_command_invocation_executable_sources_migration,
)
from .connection import configure_connection, ensure_meta_tables
from .constants import (
    FOREIGN_KEY_CHECK_QUERY,
    INTEGRITY_CHECK_OK,
    INTEGRITY_CHECK_QUERY,
    JOURNAL_STATUS_COMPLETED,
    JOURNAL_STATUS_FAILED,
    JOURNAL_STATUS_RUNNING,
    MIGRATION_COMPONENT_LOCAL_RUNTIME,
)
from .embedding_chunk_generation_rebase import (
    apply_embedding_chunk_generation_rebase_migration,
)
from .legacy_agent_experience_job_cutover import (
    apply_legacy_agent_experience_job_cutover,
)
from .legacy_fact_job_cutover import apply_legacy_fact_job_cutover
from .legacy_goal_worker_convergence import (
    apply_legacy_goal_worker_convergence_migration,
)
from .legacy_memory_agent_cutover import apply_legacy_memory_agent_cutover
from .legacy_memory_restore import apply_legacy_memory_schema_restore_migration
from .memory_agent_triggers import apply_memory_agent_trigger_migration
from .phase3 import (
    apply_public_event_projection_alignment_migration,
    apply_remove_socket_resource_scope_migration,
    apply_runtime_process_lock_ledger_migration,
    apply_tool_runtime_resource_events_migration,
    apply_tool_runtime_resource_migration,
    apply_tooling_contract_migration,
    apply_workspace_lock_owner_token_migration,
)
from .post_action_pipeline_process_kind import (
    apply_post_action_pipeline_process_kind_migration,
)
from .process_status_genericization import (
    apply_process_status_genericization_migration,
)
from .remove_intervention_mode import apply_remove_intervention_mode_migration
from .schema_probes import (
    agent_memory_run_logs_schema_is_current,
    manifest_runtime_authority_is_current,
)
from .screenshot_era_agent_cutover import apply_screenshot_era_agent_cutover
from .source_records_memory import apply_source_records_memory_migration
from .specs import MigrationError, MigrationSpec
from .sql_script import (
    PreparedSqlScript,
    SqlScriptContractError,
    execute_sql_statements,
    prepare_sql_script,
)
from .suggestion_critic_process_kind import (
    apply_suggestion_critic_process_kind_migration,
)
from .suggestion_reaction_text_domain import (
    apply_suggestion_reaction_text_domain_migration,
)
from .tool_output_storage_kind import apply_tool_output_storage_kind_migration
from .unified_memory_triggers import apply_unified_memory_trigger_migration
from .workspace_folders_in_projects import apply_workspace_folders_in_projects_migration

logger = logging.getLogger(__name__)
MANIFEST_RUNTIME_AUTHORITY_VERSION = 37
AGENT_MEMORY_RUN_LOGS_VERSION = 40
INTERRUPTED_MIGRATION_ERROR = "Interrupted before completion; superseded by retry"
# 0066 shipped with script-owned BEGIN/COMMIT. Keep its immutable checksum while
# executing those statements under the runner-owned transaction contract.
LEGACY_SCRIPT_TRANSACTION_VERSIONS = frozenset({66})
# These versions shipped on the pre-reset context branch and are intentionally
# absent from this build. Migration 0061 restores their compatible schema surface.
RETIRED_CONTEXT_SCHEMA_VERSIONS = frozenset(range(53, 61))
RETIRED_MIGRATION_IDENTITIES = frozenset(
    {
        (
            75,
            "0075_formal_tool_step_output_envelope.sql",
            "b35770ae305a15e717670f2083387d301576a7d76c64a8415b6c890d4e2d74b6",
        ),
        (
            75,
            "0075_formal_tool_step_cutover.sql",
            "d99a61325c2278fd4290af2130af90d66de85dc1283e34602b36127393e361db",
        ),
    }
)


def apply_migrations(
    db_path: Path,
    busy_timeout_ms: int,
    migrations: tuple[MigrationSpec, ...],
) -> None:
    _validate_busy_timeout(busy_timeout_ms)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        ensure_meta_tables(connection)
        _validate_current_schema_version(
            connection=connection,
            migrations=migrations,
        )
        for migration in migrations:
            _apply_single_migration(connection=connection, migration=migration)


def verify_database_integrity(db_path: Path, busy_timeout_ms: int) -> None:
    _validate_busy_timeout(busy_timeout_ms)
    with sqlite3.connect(db_path) as connection:
        configure_connection(connection=connection, busy_timeout_ms=busy_timeout_ms)
        integrity_rows = connection.execute(INTEGRITY_CHECK_QUERY).fetchall()
        if not integrity_rows:
            raise MigrationError("PRAGMA integrity_check returned no rows")
        first_result = str(integrity_rows[0][0])
        if first_result != INTEGRITY_CHECK_OK:
            raise MigrationError(f"SQLite integrity check failed: {first_result}")
        fk_rows = connection.execute(FOREIGN_KEY_CHECK_QUERY).fetchall()
        if fk_rows:
            raise MigrationError(
                f"SQLite foreign_key_check detected {len(fk_rows)} violation(s)"
            )


def _validate_busy_timeout(busy_timeout_ms: int) -> None:
    if busy_timeout_ms <= 0:
        raise MigrationError("LOCAL_DB_BUSY_TIMEOUT_MS must be a positive integer")


def _load_applied_schema_state(
    connection: sqlite3.Connection,
) -> tuple[int, str, str] | None:
    row = connection.execute(
        """
        SELECT current_version, migration_name, checksum
        FROM schema_versions
        WHERE component = ?
        """,
        (MIGRATION_COMPONENT_LOCAL_RUNTIME,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]), str(row[2])


def _apply_single_migration(
    connection: sqlite3.Connection, migration: MigrationSpec
) -> None:
    try:
        prepared_script = prepare_sql_script(
            migration.sql,
            strip_transaction_control=(
                migration.version in LEGACY_SCRIPT_TRANSACTION_VERSIONS
            ),
        )
    except SqlScriptContractError as exc:
        raise MigrationError(
            f"Invalid transaction contract in migration {migration.name}"
        ) from exc
    applied_state = _load_applied_schema_state(connection)
    if applied_state is not None:
        applied_version, _applied_name, applied_checksum = applied_state
        if applied_version > migration.version:
            return
        if applied_version == migration.version:
            if applied_checksum != migration.checksum_sha256:
                if migration.version == MANIFEST_RUNTIME_AUTHORITY_VERSION:
                    if manifest_runtime_authority_is_current(connection):
                        _sync_current_schema_checksum(
                            connection=connection,
                            migration=migration,
                        )
                        return
                    from_version = applied_version
                elif migration.version == AGENT_MEMORY_RUN_LOGS_VERSION:
                    if agent_memory_run_logs_schema_is_current(connection):
                        _sync_current_schema_checksum(
                            connection=connection,
                            migration=migration,
                        )
                        return
                    from_version = applied_version
                else:
                    raise MigrationError(
                        "Schema checksum mismatch for version "
                        f"{migration.version}: expected {migration.checksum_sha256}, got {applied_checksum}"
                    )
            else:
                return
        from_version = applied_version
    else:
        from_version = 0

    run_id = str(uuid.uuid4())
    connection.execute(
        """
        UPDATE migration_journal
        SET completed_at = COALESCE(
                completed_at,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            ),
            status = ?,
            error_message = COALESCE(error_message, ?)
        WHERE migration_name = ?
          AND component = ?
          AND status = ?
        """,
        (
            JOURNAL_STATUS_FAILED,
            INTERRUPTED_MIGRATION_ERROR,
            migration.name,
            MIGRATION_COMPONENT_LOCAL_RUNTIME,
            JOURNAL_STATUS_RUNNING,
        ),
    )
    connection.execute(
        """
        INSERT INTO migration_journal(
            migration_run_id,
            migration_name,
            component,
            from_version,
            to_version,
            started_at,
            status,
            app_version,
            checksum
        ) VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?)
        """,
        (
            run_id,
            migration.name,
            MIGRATION_COMPONENT_LOCAL_RUNTIME,
            from_version,
            migration.version,
            JOURNAL_STATUS_RUNNING,
            APP_VERSION,
            migration.checksum_sha256,
        ),
    )
    connection.commit()

    try:
        _set_foreign_keys_for_migration(
            connection=connection,
            enabled=not prepared_script.requires_foreign_keys_disabled,
        )
        with immediate_transaction(connection):
            _execute_migration(
                connection=connection,
                migration=migration,
                prepared_script=prepared_script,
            )
            connection.execute(
                """
                INSERT INTO schema_versions(
                    schema_version_id,
                    component,
                    current_version,
                    applied_at,
                    app_version,
                    migration_name,
                    checksum
                ) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    schema_version_id = excluded.schema_version_id,
                    current_version = excluded.current_version,
                    applied_at = excluded.applied_at,
                    app_version = excluded.app_version,
                    migration_name = excluded.migration_name,
                    checksum = excluded.checksum
                """,
                (
                    str(uuid.uuid4()),
                    MIGRATION_COMPONENT_LOCAL_RUNTIME,
                    migration.version,
                    APP_VERSION,
                    migration.name,
                    migration.checksum_sha256,
                ),
            )
            connection.execute(
                """
                UPDATE migration_journal
                SET completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    status = ?
                WHERE migration_run_id = ?
                """,
                (JOURNAL_STATUS_COMPLETED, run_id),
            )
    except Exception as exc:  # noqa: BLE001 - transaction boundary must roll back all migration failures
        _set_foreign_keys_for_migration(connection=connection, enabled=True)
        with connection:
            connection.execute(
                """
                UPDATE migration_journal
                SET completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    status = ?,
                    error_message = ?
                WHERE migration_run_id = ?
                """,
                (JOURNAL_STATUS_FAILED, str(exc), run_id),
            )
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"Failed to apply migration {migration.name}") from exc
    _set_foreign_keys_for_migration(connection=connection, enabled=True)

    logger.info("Applied local runtime migration: %s", migration.name)


def _execute_migration(
    connection: sqlite3.Connection,
    migration: MigrationSpec,
    prepared_script: PreparedSqlScript,
) -> None:
    if migration.version == 7:
        apply_local_action_state_migration(connection)
        return
    if migration.version == 8:
        return
    if migration.version == 9:
        apply_tooling_contract_migration(connection)
        return
    if migration.version == 10:
        apply_tool_runtime_resource_migration(connection)
        return
    if migration.version == 11:
        apply_tool_runtime_resource_events_migration(connection)
        return
    if migration.version == 12:
        apply_runtime_process_lock_ledger_migration(connection)
        return
    if migration.version == 13:
        apply_remove_socket_resource_scope_migration(connection)
        return
    if migration.version == 14:
        apply_workspace_lock_owner_token_migration(connection)
        return
    if migration.version == 15:
        apply_public_event_projection_alignment_migration(connection)
        return
    if migration.version == 16:
        apply_action_status_alignment_migration(connection)
        return
    if migration.version == 20:
        apply_process_status_genericization_migration(connection)
        return
    if migration.version == 21:
        apply_action_job_payload_enqueued_at_migration(connection)
        return
    if migration.version == 22:
        apply_suggestion_critic_process_kind_migration(connection)
        return
    if migration.version == 23:
        apply_activity_summary_fact_followup_migration(connection)
        return
    if migration.version == 28:
        apply_remove_intervention_mode_migration(connection)
        return
    if migration.version == 29:
        apply_command_invocation_executable_sources_migration(
            connection,
            migration_statements=prepared_script.statements,
        )
        return
    if migration.version == 31:
        apply_post_action_pipeline_process_kind_migration(connection)
        return
    if migration.version == 35:
        apply_suggestion_reaction_text_domain_migration(connection)
        return
    if migration.version in (36, 37) and manifest_runtime_authority_is_current(
        connection
    ):
        return
    if migration.version == 61:
        apply_legacy_memory_schema_restore_migration(connection)
        return
    if migration.version == 66:
        apply_action_file_read_memory_migration(
            connection,
            migration_statements=prepared_script.statements,
        )
        return
    if migration.version == 74:
        apply_tool_output_storage_kind_migration(
            connection,
            migration_statements=prepared_script.statements,
        )
        return
    if migration.version == 80:
        apply_memory_agent_trigger_migration(
            connection,
            migration_statements=prepared_script.statements,
        )
        return
    if migration.version == 81:
        apply_legacy_fact_job_cutover(connection)
        return
    if migration.version == 82:
        apply_action_creation_cutover_migration(connection)
        return
    if migration.version == 84:
        apply_embedding_chunk_generation_rebase_migration(connection)
        return
    if migration.version == 85:
        apply_legacy_goal_worker_convergence_migration(connection)
        return
    if migration.version == 86:
        apply_action_user_process_ownership_migration(connection)
        return
    if migration.version == 89:
        apply_action_user_state_contract_migration(connection)
        return
    if migration.version == 93:
        apply_action_subagent_processes_migration(
            connection,
            migration_statements=prepared_script.statements,
        )
        return
    if migration.version == 99:
        apply_unified_memory_trigger_migration(
            connection,
            migration_statements=prepared_script.statements,
        )
        return
    if migration.version == 100:
        apply_legacy_agent_experience_job_cutover(connection)
        return
    if migration.version == 101:
        apply_legacy_memory_agent_cutover(connection)
        return
    if migration.version == 107:
        apply_action_assistant_messages_migration(
            connection, migration_statements=prepared_script.statements
        )
        return
    if migration.version == 118:
        apply_source_records_memory_migration(
            connection, migration_statements=prepared_script.statements
        )
        return
    if migration.version == 125:
        apply_workspace_folders_in_projects_migration(connection)
        return
    if migration.version == 102:
        apply_screenshot_era_agent_cutover(connection)
        return
    execute_sql_statements(connection, prepared_script.statements)


def _set_foreign_keys_for_migration(
    *,
    connection: sqlite3.Connection,
    enabled: bool,
) -> None:
    connection.execute(f"PRAGMA foreign_keys = {'ON' if enabled else 'OFF'}")
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    expected = 1 if enabled else 0
    if row is None or int(row[0]) != expected:
        state = "enabled" if enabled else "disabled"
        raise MigrationError(f"SQLite foreign keys could not be {state}")


def _sync_current_schema_checksum(
    *,
    connection: sqlite3.Connection,
    migration: MigrationSpec,
) -> None:
    connection.execute(
        """
        UPDATE schema_versions
        SET checksum = ?,
            migration_name = ?,
            applied_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE component = ? AND current_version = ?
        """,
        (
            migration.checksum_sha256,
            migration.name,
            MIGRATION_COMPONENT_LOCAL_RUNTIME,
            migration.version,
        ),
    )


def _validate_current_schema_version(
    *,
    connection: sqlite3.Connection,
    migrations: tuple[MigrationSpec, ...],
) -> None:
    applied_state = _load_applied_schema_state(connection)
    if applied_state is None:
        return
    applied_version, migration_name, applied_checksum = applied_state
    known_versions = frozenset(migration.version for migration in migrations)
    if not known_versions:
        raise MigrationError(
            "Migration plan must not be empty for an existing database"
        )
    if applied_version in known_versions:
        return
    retired_identity = (applied_version, migration_name, applied_checksum)
    if retired_identity in RETIRED_MIGRATION_IDENTITIES:
        logger.warning(
            "Local runtime schema is at retired migration version %s (%s); "
            "continuing to the current migration plan.",
            applied_version,
            migration_name,
        )
        return
    if applied_version in RETIRED_CONTEXT_SCHEMA_VERSIONS:
        logger.warning(
            "Local runtime schema is at retired context migration version %s "
            "(%s); continuing to the compatibility restore migration.",
            applied_version,
            migration_name,
        )
        return
    plan_start = min(known_versions)
    if applied_version < plan_start:
        return
    plan_tip = max(known_versions)
    if applied_version > plan_tip:
        raise MigrationError(
            "Local runtime schema version "
            f"{applied_version} ({migration_name}) is newer than migration "
            f"plan tip {plan_tip}; refusing to start"
        )
    raise MigrationError(
        "Local runtime schema version "
        f"{applied_version} ({migration_name}) is not recognized by this build"
    )

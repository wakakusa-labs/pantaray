from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

MIGRATION_VERSION_PREFIX_LENGTH = 4


@dataclass(frozen=True)
class MigrationSpec:
    version: int
    name: str
    sql: str

    @property
    def checksum_sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


class MigrationError(RuntimeError):
    """Raised when local runtime migrations cannot be applied safely."""


def load_default_migrations() -> tuple[MigrationSpec, ...]:
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "migrations"
    migration_names = (
        "0001_core_runtime.sql",
        "0002_action_history.sql",
        "0003_activity_capture.sql",
        "0004_action_runtime_support.sql",
        "0005_memory_artifacts.sql",
        "0006_scheduler_security.sql",
        "0007_local_action_state.sql",
        "0008_local_public_event_projection.sql",
        "0009_phase3_tooling_contract.sql",
        "0010_phase3_resource_cleanup.sql",
        "0011_phase3_resource_cleanup_events.sql",
        "0012_runtime_process_lock_ledger.sql",
        "0013_remove_socket_resource_scope.sql",
        "0014_workspace_lock_owner_token.sql",
        "0015_public_event_projection_alignment.sql",
        "0016_action_status_alignment.sql",
        "0017_command_invocation_audits.sql",
        "0018_memory_record_references.sql",
        "0019_job_logical_keys.sql",
        "0020_process_status_genericization.sql",
        "0021_action_job_payload_enqueued_at.sql",
        "0022_suggestion_critic_process_kind.sql",
        "0023_activity_summary_fact_followup.sql",
        "0024_drop_screen_captures.sql",
        "0025_scheduler_job_runs_unique_window.sql",
        "0026_message_only_action_invariant.sql",
        "0027_action_step_latest_resolution.sql",
        "0028_remove_intervention_mode.sql",
        "0029_command_invocation_executable_sources.sql",
        "0030_nullable_command_invocation_approval.sql",
        "0031_post_action_pipeline_process_kind.sql",
        "0032_action_terminal_runtime_repairs.sql",
        "0033_drop_feature_readiness.sql",
        "0034_memory_search_row_fts.sql",
        "0035_suggestion_reaction_text_domain.sql",
        "0036_virtual_workspace_manifest.sql",
        "0037_manifest_runtime_authority.sql",
        "0038_process_paused_public_events.sql",
        "0039_action_resume_requested_event.sql",
        "0040_agent_insight_updates.sql",
        "0041_remove_synthetic_tool_steps.sql",
        "0042_local_workspace_paths.sql",
        "0043_long_term_state_logical_source.sql",
        "0044_read_access_preferences.sql",
        "0045_drop_activity_response_text.sql",
        "0046_execution_session_read_access_scope.sql",
        "0047_interrupted_approval_sessions.sql",
        "0048_workspace_manifest_context_snapshot.sql",
        "0049_agent_run_step_tables_repair.sql",
        "0050_agent_thinking_columns.sql",
        "0051_suggestion_handoff_context.sql",
        "0052_backfill_fact_brief_runs.sql",
        "0061_restore_legacy_memory_schema.sql",
        "0062_memory_catalog.sql",
        "0063_optional_insight_update_provenance.sql",
        "0064_memory_catalog_maintenance.sql",
        "0065_fact_activity_checkpoint.sql",
        "0066_action_file_read_memory.sql",
        "0067_fact_activity_checkpoint_evidence.sql",
        "0068_workspace_project_order.sql",
        "0069_suggestion_react_steps.sql",
        "0070_memory_revision_profile_brief.sql",
        "0071_memory_semantic_index.sql",
        "0072_agent_experience.sql",
        "0073_action_user_request_steps.sql",
        "0074_tool_output_storage_kind.sql",
        "0076_remove_legacy_tool_action_steps.sql",
        "0077_retire_legacy_memory_triggers.sql",
        "0078_insight_activity_summary_provenance.sql",
        "0079_fact_activity_consumptions.sql",
        "0080_memory_agent_triggers.sql",
        "0081_legacy_fact_job_cutover.sql",
        "0082_action_creation_cutover.sql",
        "0083_embedding_chunk_generation_identity.sql",
        "0084_embedding_chunk_generation_rebase.sql",
        "0085_legacy_goal_worker_convergence.sql",
        "0086_action_user_process_ownership.sql",
        "0087_action_public_event_namespace.sql",
        "0088_owner_queued_job_poll_index.sql",
        "0089_action_user_state_contract.sql",
        "0090_action_run_timeline_index.sql",
        "0091_action_conversation_query_indexes.sql",
        "0092_action_history_recency_indexes.sql",
        "0093_action_subagent_processes.sql",
        "0094_action_subagent_resource_claims.sql",
        "0095_drop_legacy_goal_worker_schema.sql",
        "0096_zanei_context.sql",
        "0097_drop_structured_work_state.sql",
        "0098_short_insight_outputs.sql",
        "0099_unified_memory_triggers.sql",
        "0100_legacy_agent_experience_job_cutover.sql",
        "0101_legacy_memory_agent_cutover.sql",
        "0102_screenshot_era_agent_cutover.sql",
        "0103_action_approval_modes.sql",
        "0104_drop_tool_redactions.sql",
        "0105_drop_retention_jobs.sql",
        "0106_action_initial_approval_mode.sql",
        "0107_action_assistant_messages.sql",
        "0108_assistant_conversation_index.sql",
        "0109_suggestion_reply_identity.sql",
        "0110_command_network_preferences.sql",
        "0111_command_workspace_write_grants.sql",
        "0112_local_owner.sql",
        "0113_local_embedding_profile.sql",
        "0114_action_tool_call_identity.sql",
        "0115_memory_fragment_trigram_index.sql",
        "0116_action_provider_turn_identity.sql",
        "0117_source_records.sql",
        "0118_source_records_memory.sql",
        "0119_approved_folder_manifest_roots.sql",
        "0120_insight_source_cursor.sql",
        "0121_memory_note_source.sql",
        "0122_suggestion_delivery_state.sql",
    )
    return tuple(
        MigrationSpec(
            version=_parse_migration_version(name),
            name=name,
            sql=(migrations_dir / name).read_text(encoding="utf-8"),
        )
        for name in migration_names
    )


def _parse_migration_version(migration_name: str) -> int:
    prefix = migration_name[:MIGRATION_VERSION_PREFIX_LENGTH]
    separator = migration_name[
        MIGRATION_VERSION_PREFIX_LENGTH : MIGRATION_VERSION_PREFIX_LENGTH + 1
    ]
    if separator != "_" or not prefix.isdecimal():
        raise MigrationError(f"Invalid local runtime migration name: {migration_name}")
    return int(prefix)

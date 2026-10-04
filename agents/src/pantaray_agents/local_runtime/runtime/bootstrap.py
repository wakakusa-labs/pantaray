from __future__ import annotations

import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ...utils.url_validation import parse_network_url
from ..app_runtime_verification import (
    LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV,
    load_and_verify_app_runtime_python_from_env,
)
from ..artifacts.bootstrap import bootstrap_local_artifact_store
from ..embedding_local import (
    LocalEmbeddingUnavailableError,
    load_local_embedding_model,
)
from ..memory_catalog.artifact_recovery import recover_artifact_revision_intents
from ..memory_catalog.connection import open_memory_catalog_connection
from ..memory_catalog.lifecycle import (
    process_pending_artifact_deletions,
    remove_abandoned_preparing_nodes,
)
from ..memory_catalog.observability import refresh_memory_catalog_backlog_metrics
from ..memory_catalog.reconciler import reconcile_memory_catalog
from ..memory_catalog.run_workspace import cleanup_orphaned_memory_run_workspaces
from ..storage.migrations import (
    MigrationError,
    apply_migrations,
    load_default_migrations,
    record_runtime_recovery_run,
    repair_inflight_jobs_for_startup,
)
from ..storage.transactions import immediate_transaction
from ..tooling.bootstrap import bootstrap_local_tooling_catalog
from ..tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_startup,
)
from ..tooling.tool_result_recovery import reconcile_tool_results_for_startup
from .activity_summary_recovery import (
    recover_interrupted_activity_summary_scheduler_runs,
)
from .runtime_env import (
    LOCAL_ARTIFACT_ROOT_ENV,
    LOCAL_DB_BUSY_TIMEOUT_MS_ENV,
    LOCAL_DB_PATH_ENV,
    read_local_runtime_artifact_root,
    read_local_runtime_db_config,
)

LLM_PROXY_URL_ENV = "LLM_PROXY_URL"
WEB_TOOLS_PROXY_URL_ENV = "WEB_TOOLS_PROXY_URL"
FORBIDDEN_PROVIDER_SECRET_ENVS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "TAVILY_API_KEY",
)
logger = logging.getLogger(__name__)
MEMORY_REPAIR_STARTUP_SCAN_LIMIT = 200


@dataclass(frozen=True, slots=True)
class LocalRuntimeBootstrapConfig:
    db_path: Path
    busy_timeout_ms: int
    artifact_root: Path
    app_runtime_python: Path


def is_local_runtime_enabled() -> bool:
    return True


def _validate_cloud_boundary_env() -> None:
    # Absent while Pantaray account login is disabled: there is no Cloud route then.
    for name in (LLM_PROXY_URL_ENV, WEB_TOOLS_PROXY_URL_ENV):
        value = os.getenv(name)
        if value is None or not value.strip():
            continue
        try:
            parse_network_url(value, env_name=name, allow_path=True)
        except ValueError as exc:
            raise MigrationError(str(exc)) from exc
    leaked_secrets = [env for env in FORBIDDEN_PROVIDER_SECRET_ENVS if os.getenv(env)]
    if leaked_secrets:
        names = ", ".join(leaked_secrets)
        raise MigrationError(
            "Local runtime must not hold provider credentials: "
            f"forbidden env set [{names}]"
        )


def prepare_local_runtime_bootstrap_config() -> LocalRuntimeBootstrapConfig:
    _validate_cloud_boundary_env()
    db_path, timeout_ms = read_local_runtime_db_config()
    artifact_root = read_local_runtime_artifact_root()
    app_runtime_python = load_and_verify_app_runtime_python_from_env()
    return LocalRuntimeBootstrapConfig(
        db_path=db_path.resolve(),
        busy_timeout_ms=timeout_ms,
        artifact_root=artifact_root,
        app_runtime_python=app_runtime_python,
    )


def run_local_runtime_bootstrap(
    *,
    bootstrap_config: LocalRuntimeBootstrapConfig,
    recovered_runtime_lock_count: int,
    resumed_user_erasure_count: int,
) -> None:
    bootstrap_local_artifact_store(root_path=bootstrap_config.artifact_root)
    workspace_cleanup = cleanup_orphaned_memory_run_workspaces(
        artifact_root=bootstrap_config.artifact_root
    )
    recovered_memory_intent_count = recover_artifact_revision_intents(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
        artifact_root=bootstrap_config.artifact_root,
    )
    with open_memory_catalog_connection(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    ) as connection:
        with immediate_transaction(connection):
            recovered_memory_deletion_count = process_pending_artifact_deletions(
                connection=connection,
                artifact_root=bootstrap_config.artifact_root,
            )
    # Startup owns one pass over crash residue before the worker accepts jobs.
    memory_repairs_enqueued, memory_repairs_completed = reconcile_memory_catalog(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
        artifact_root=bootstrap_config.artifact_root,
        scan_limit=MEMORY_REPAIR_STARTUP_SCAN_LIMIT,
    )
    recovered_activity_summary_scheduler_run_count = (
        recover_interrupted_activity_summary_scheduler_runs(
            db_path=bootstrap_config.db_path,
            busy_timeout_ms=bootstrap_config.busy_timeout_ms,
        )
    )
    from .action_startup_recovery import recover_interrupted_action_runs_for_startup
    from .action_subagent_startup_recovery import (
        recover_action_subagent_children_for_startup,
    )

    # Children settle first, so a root a live child still owns is never cleaned.
    child_recovery = recover_action_subagent_children_for_startup(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    )
    recover_interrupted_action_runs_for_startup(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
        preserved_roots=child_recovery.preserved_roots,
    )
    repaired_job_count = repair_inflight_jobs_for_startup(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    )
    with open_memory_catalog_connection(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    ) as connection:
        with immediate_transaction(connection):
            abandoned_preparing_node_count = remove_abandoned_preparing_nodes(
                connection=connection
            )
            refresh_memory_catalog_backlog_metrics(connection=connection)
    recovered_tool_resource_count = reconcile_tool_runtime_resources_for_startup(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    )
    tool_result_recovery = reconcile_tool_results_for_startup(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    )
    recovery_note = (
        "startup deterministic recovery checkpoint: "
        f"repaired_inflight_jobs={repaired_job_count}; "
        f"recovered_activity_summary_scheduler_runs="
        f"{recovered_activity_summary_scheduler_run_count}; "
        f"recovered_runtime_locks={recovered_runtime_lock_count}; "
        f"recovered_tool_resources={recovered_tool_resource_count}; "
        f"removed_orphaned_tool_result_files="
        f"{tool_result_recovery.removed_file_count}; "
        f"removed_empty_tool_result_directories="
        f"{tool_result_recovery.removed_directory_count}; "
        f"unknown_tool_result_entries="
        f"{tool_result_recovery.unknown_entry_count}; "
        f"recovered_memory_intents={recovered_memory_intent_count}; "
        f"recovered_memory_deletions={recovered_memory_deletion_count}; "
        f"removed_orphaned_memory_workspaces={workspace_cleanup.removed_count}; "
        f"failed_orphaned_memory_workspace_cleanup={workspace_cleanup.failed_count}; "
        f"abandoned_preparing_nodes={abandoned_preparing_node_count}"
        f"; settled_subagent_children={child_recovery.settled_child_count}"
        f"; preserved_subagent_roots={len(child_recovery.preserved_roots)}"
        f"; collected_child_invocations={child_recovery.collected_invocation_count}"
        f"; blocked_subagent_children={child_recovery.blocked_child_count}"
        f"; unreadable_subagent_children={child_recovery.unreadable_child_count}"
        f"; resumed_user_erasures={resumed_user_erasure_count}"
        f"; memory_repairs_enqueued={memory_repairs_enqueued}"
        f"; memory_repairs_completed={memory_repairs_completed}"
    )
    record_runtime_recovery_run(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
        note=recovery_note,
    )
    logger.info(
        "local runtime startup recovery completed",
        extra={
            "recovered_memory_intent_count": recovered_memory_intent_count,
            "recovered_memory_deletion_count": recovered_memory_deletion_count,
            "removed_orphaned_memory_workspace_count": (
                workspace_cleanup.removed_count
            ),
            "failed_orphaned_memory_workspace_cleanup_count": (
                workspace_cleanup.failed_count
            ),
            "abandoned_preparing_node_count": abandoned_preparing_node_count,
            "resumed_user_erasure_count": resumed_user_erasure_count,
            "memory_repairs_enqueued": memory_repairs_enqueued,
            "memory_repairs_completed": memory_repairs_completed,
            "removed_orphaned_tool_result_file_count": (
                tool_result_recovery.removed_file_count
            ),
            "removed_empty_tool_result_directory_count": (
                tool_result_recovery.removed_directory_count
            ),
            "preserved_tool_result_file_count": (
                tool_result_recovery.preserved_file_count
            ),
            "unknown_tool_result_entry_count": (
                tool_result_recovery.unknown_entry_count
            ),
        },
    )
    bootstrap_local_tooling_catalog(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
    )
    _verify_local_embedding_model()


def _verify_local_embedding_model() -> None:
    """Decide once, at startup, whether semantic memory search can run.

    The loader logs the model it accepted or the reason it refused one. A
    missing or mismatched model is not a startup failure: the rest of the
    runtime works and memory search reports `provider_unavailable`.
    """
    with suppress(LocalEmbeddingUnavailableError):
        load_local_embedding_model()


def apply_local_runtime_schema(
    *,
    bootstrap_config: LocalRuntimeBootstrapConfig,
) -> None:
    apply_migrations(
        db_path=bootstrap_config.db_path,
        busy_timeout_ms=bootstrap_config.busy_timeout_ms,
        migrations=load_default_migrations(),
    )


__all__ = [
    "LOCAL_ARTIFACT_ROOT_ENV",
    "LOCAL_DB_BUSY_TIMEOUT_MS_ENV",
    "LOCAL_DB_PATH_ENV",
    "LLM_PROXY_URL_ENV",
    "LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV",
    "LocalRuntimeBootstrapConfig",
    "WEB_TOOLS_PROXY_URL_ENV",
    "apply_local_runtime_schema",
    "is_local_runtime_enabled",
    "prepare_local_runtime_bootstrap_config",
    "read_local_runtime_artifact_root",
    "read_local_runtime_db_config",
    "run_local_runtime_bootstrap",
]

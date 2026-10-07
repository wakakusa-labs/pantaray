"""Local runtime bootstrap utilities."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "bootstrap_local_artifact_store": (".artifacts", "bootstrap_local_artifact_store"),
    "write_managed_file_artifact": (".artifacts", "write_managed_file_artifact"),
    "MemoryKeyParts": (".memory_references", "MemoryKeyParts"),
    "build_memory_key": (".memory_references", "build_memory_key"),
    "split_memory_key": (".memory_references", "split_memory_key"),
    "RecoveryAuditEvent": (".recovery_audit", "RecoveryAuditEvent"),
    "RecoveryAuditScope": (".recovery_audit", "RecoveryAuditScope"),
    "RecoveryAuditSeverity": (".recovery_audit", "RecoveryAuditSeverity"),
    "list_action_recovery_audit_events": (
        ".recovery_audit",
        "list_action_recovery_audit_events",
    ),
    "list_invocation_recovery_audit_events": (
        ".recovery_audit",
        "list_invocation_recovery_audit_events",
    ),
    "list_runtime_recovery_audit_events": (
        ".recovery_audit",
        "list_runtime_recovery_audit_events",
    ),
    "is_local_runtime_enabled": (".runtime.bootstrap", "is_local_runtime_enabled"),
    "rebuild_memory_artifact_blocks_fts": (
        ".storage.artifact_block_fts",
        "rebuild_memory_artifact_blocks_fts",
    ),
    "search_memory_artifact_blocks": (
        ".storage.artifact_block_fts",
        "search_memory_artifact_blocks",
    ),
    "get_import_run": (".storage.import_runs", "get_import_run"),
    "list_import_run_sources": (".storage.import_runs", "list_import_run_sources"),
    "enqueue_fts_rebuild_job": (
        ".storage.projection_rebuild",
        "enqueue_fts_rebuild_job",
    ),
    "rebuild_memory_artifact_fts_projection": (
        ".storage.projection_rebuild",
        "rebuild_memory_artifact_fts_projection",
    ),
    "run_next_pending_fts_rebuild_job": (
        ".storage.projection_rebuild",
        "run_next_pending_fts_rebuild_job",
    ),
    "append_local_process_event": (
        ".runtime.process_events",
        "append_local_process_event",
    ),
    "read_local_process_events_after": (
        ".runtime.process_events",
        "read_local_process_events_after",
    ),
    "stop_local_action_worker_daemon": (
        ".runtime.worker_daemon",
        "stop_local_action_worker_daemon",
    ),
    "start_local_runtime_if_enabled": (
        ".runtime.lifecycle",
        "start_local_runtime_if_enabled",
    ),
    "stop_local_runtime_if_enabled": (
        ".runtime.lifecycle",
        "stop_local_runtime_if_enabled",
    ),
    "LocalSuggestionStateRepository": (
        ".suggestion_state.repository",
        "LocalSuggestionStateRepository",
    ),
}


def __getattr__(name: str) -> object:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute_name = target
    module = import_module(module_name, __name__)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value


__all__ = list(_EXPORTS)

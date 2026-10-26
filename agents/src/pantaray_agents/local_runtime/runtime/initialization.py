from __future__ import annotations

import logging
from dataclasses import dataclass

from ..memory_catalog.cutover import run_memory_catalog_cutover
from ..memory_catalog.erasure import resume_pending_user_erasures
from .bootstrap import (
    LocalRuntimeBootstrapConfig,
    apply_local_runtime_schema,
    prepare_local_runtime_bootstrap_config,
    run_local_runtime_bootstrap,
)
from .identity import register_logged_out_owner
from .local_owner import ensure_logged_out_owner
from .local_runtime_generation_cutover import reset_legacy_local_runtime_generation
from .process_lock import release_runtime_process_lock
from .runtime_lock_coordinator import (
    RuntimeProcessLockLease,
    acquire_runtime_lock_for_startup,
    attach_runtime_lock_lease,
    release_runtime_lock_lease,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InitializedLocalRuntime:
    config: LocalRuntimeBootstrapConfig
    lock_lease: RuntimeProcessLockLease


def initialize_local_runtime() -> InitializedLocalRuntime:
    config = prepare_local_runtime_bootstrap_config()
    lock_acquisition = acquire_runtime_lock_for_startup(
        db_path=config.db_path,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    lock_lease: RuntimeProcessLockLease | None = None
    try:
        reset_legacy_local_runtime_generation(
            db_path=config.db_path,
            busy_timeout_ms=config.busy_timeout_ms,
            artifact_root=config.artifact_root,
            runtime_lock=lock_acquisition.runtime_lock,
        )
        # No whole-file integrity_check here: it reads every page (135 s on a
        # 4.9 GB store), past the window the app waits for this process.
        apply_local_runtime_schema(bootstrap_config=config)
        register_logged_out_owner(
            ensure_logged_out_owner(
                db_path=config.db_path,
                busy_timeout_ms=config.busy_timeout_ms,
            )
        )
        lock_attachment = attach_runtime_lock_lease(
            db_path=config.db_path,
            busy_timeout_ms=config.busy_timeout_ms,
            runtime_lock=lock_acquisition.runtime_lock,
            event_type="startup_runtime_lock_acquired",
            event_message="startup runtime global lock acquired",
        )
        lock_lease = lock_attachment.lease
        resumed_user_erasure_count = resume_pending_user_erasures(
            db_path=config.db_path,
            busy_timeout_ms=config.busy_timeout_ms,
            artifact_root=config.artifact_root,
        )
        run_memory_catalog_cutover(
            db_path=config.db_path,
            busy_timeout_ms=config.busy_timeout_ms,
            artifact_root=config.artifact_root,
        )
        run_local_runtime_bootstrap(
            bootstrap_config=config,
            recovered_runtime_lock_count=lock_attachment.recovered_resource_count,
            resumed_user_erasure_count=resumed_user_erasure_count,
        )
    except Exception:
        if lock_lease is None:
            release_runtime_process_lock(runtime_lock=lock_acquisition.runtime_lock)
        else:
            _release_lock(config=config, lock_lease=lock_lease)
        raise
    if lock_lease is None:
        raise RuntimeError("runtime lock lease was not attached")
    return InitializedLocalRuntime(config=config, lock_lease=lock_lease)


def release_initialized_local_runtime(runtime: InitializedLocalRuntime) -> None:
    _release_lock(config=runtime.config, lock_lease=runtime.lock_lease)


def _release_lock(
    *,
    config: LocalRuntimeBootstrapConfig,
    lock_lease: RuntimeProcessLockLease,
) -> None:
    release_result = release_runtime_lock_lease(
        db_path=config.db_path,
        busy_timeout_ms=config.busy_timeout_ms,
        lease=lock_lease,
    )
    if release_result.warning_message is not None:
        logger.warning("%s", release_result.warning_message)

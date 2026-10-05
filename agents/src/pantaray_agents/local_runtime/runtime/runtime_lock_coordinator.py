from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .process_lock import (
    RuntimeProcessLock,
    acquire_runtime_process_lock,
    release_runtime_process_lock,
)
from .runtime_lock_repository import (
    create_runtime_lock_resource,
    list_open_runtime_lock_resource_ids,
    mark_runtime_lock_resource_cleaned,
    record_runtime_lock_event,
)
from .utc_timestamps import now_utc_iso


@dataclass(frozen=True, slots=True)
class RuntimeProcessLockLease:
    runtime_lock: RuntimeProcessLock
    resource_id: str


@dataclass(frozen=True, slots=True)
class RuntimeProcessLockAcquisition:
    runtime_lock: RuntimeProcessLock


@dataclass(frozen=True, slots=True)
class RuntimeLockLeaseAttachment:
    lease: RuntimeProcessLockLease
    recovered_resource_count: int


@dataclass(frozen=True, slots=True)
class RuntimeLockReleaseResult:
    process_lock_released: bool
    persistence_failed: bool
    warning_message: str | None


def acquire_runtime_lock_for_startup(
    *,
    db_path: Path,
    busy_timeout_ms: int,
) -> RuntimeProcessLockAcquisition:
    return RuntimeProcessLockAcquisition(
        runtime_lock=acquire_runtime_process_lock(db_path=db_path),
    )


def attach_runtime_lock_lease(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    runtime_lock: RuntimeProcessLock,
    event_type: str,
    event_message: str,
) -> RuntimeLockLeaseAttachment:
    timestamp = now_utc_iso()
    resource_id = create_runtime_lock_resource(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        lock_path=runtime_lock.lock_path,
        lock_id=runtime_lock.lock_id,
        owner_pid=runtime_lock.owner_pid,
        created_at=timestamp,
    )
    record_runtime_lock_event(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        resource_id=resource_id,
        event_type=event_type,
        message=event_message,
        created_at=timestamp,
    )
    lease = RuntimeProcessLockLease(runtime_lock=runtime_lock, resource_id=resource_id)
    recovered_resource_count = _close_previous_runtime_lock_leases(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        active_lease=lease,
    )
    return RuntimeLockLeaseAttachment(
        lease=lease,
        recovered_resource_count=recovered_resource_count,
    )


def release_runtime_lock_lease(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    lease: RuntimeProcessLockLease,
) -> RuntimeLockReleaseResult:
    timestamp = now_utc_iso()
    release_runtime_process_lock(runtime_lock=lease.runtime_lock)
    try:
        mark_runtime_lock_resource_cleaned(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=lease.resource_id,
            cleaned_at=timestamp,
        )
        record_runtime_lock_event(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=lease.resource_id,
            event_type="runtime_lock_released",
            message="runtime global lock released",
            created_at=timestamp,
        )
    except Exception as exc:
        return RuntimeLockReleaseResult(
            process_lock_released=True,
            persistence_failed=True,
            warning_message=f"runtime global lock bookkeeping failed after release: {exc}",
        )
    return RuntimeLockReleaseResult(
        process_lock_released=True,
        persistence_failed=False,
        warning_message=None,
    )


def _close_previous_runtime_lock_leases(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    active_lease: RuntimeProcessLockLease,
) -> int:
    # The caller holds the flock, so every other lease still recorded as open
    # belongs to an owner that exited without releasing it.
    closed_count = 0
    for resource_id in list_open_runtime_lock_resource_ids(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    ):
        if resource_id == active_lease.resource_id:
            continue
        timestamp = now_utc_iso()
        mark_runtime_lock_resource_cleaned(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=resource_id,
            cleaned_at=timestamp,
        )
        record_runtime_lock_event(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=resource_id,
            event_type="startup_runtime_lock_completed",
            message="startup runtime global lock cleanup completed",
            created_at=timestamp,
        )
        closed_count += 1
    return closed_count


__all__ = [
    "RuntimeProcessLockAcquisition",
    "RuntimeLockLeaseAttachment",
    "RuntimeLockReleaseResult",
    "RuntimeProcessLockLease",
    "acquire_runtime_lock_for_startup",
    "attach_runtime_lock_lease",
    "release_runtime_lock_lease",
]

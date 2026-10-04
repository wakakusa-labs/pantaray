from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .process_lock import (
    RuntimeLockRecord,
    RuntimeProcessLock,
    acquire_runtime_process_lock,
    read_runtime_lock_record,
    release_runtime_process_lock,
    runtime_process_exists,
)
from .runtime_lock_repository import (
    RuntimeLockResource,
    create_runtime_lock_resource,
    list_recoverable_runtime_lock_resources,
    mark_runtime_lock_resource_abandoned,
    mark_runtime_lock_resource_cleaned,
    mark_runtime_lock_resource_cleanup_failed,
    record_runtime_lock_event,
)
from .utc_timestamps import now_utc_iso

MAX_RUNTIME_LOCK_CLEANUP_ATTEMPTS = 3
logger = logging.getLogger(__name__)


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
    recovered_resource_count = reconcile_runtime_lock_resources_for_startup(
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


def reconcile_runtime_lock_resources_for_startup(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    active_lease: RuntimeProcessLockLease | None = None,
) -> int:
    return _reconcile_runtime_lock_resources(
        trigger="startup",
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        active_lease=active_lease,
    )


def reconcile_runtime_lock_resources_for_periodic_reaper(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    active_lease: RuntimeProcessLockLease | None = None,
) -> int:
    return _reconcile_runtime_lock_resources(
        trigger="periodic",
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        active_lease=active_lease,
    )


def _reconcile_runtime_lock_resources(
    *,
    trigger: str,
    db_path: Path,
    busy_timeout_ms: int,
    active_lease: RuntimeProcessLockLease | None,
) -> int:
    resources = list_recoverable_runtime_lock_resources(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    recovered_count = 0
    for resource in resources:
        if (
            active_lease is not None
            and resource.resource_id == active_lease.resource_id
        ):
            continue
        if _resource_is_live(resource):
            continue
        timestamp = now_utc_iso()
        try:
            _cleanup_runtime_lock_resource(resource)
        except Exception as exc:
            if resource.cleanup_attempts + 1 >= MAX_RUNTIME_LOCK_CLEANUP_ATTEMPTS:
                mark_runtime_lock_resource_abandoned(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    resource_id=resource.resource_id,
                    abandoned_at=timestamp,
                    cleanup_error=str(exc),
                )
                event_type = f"{trigger}_runtime_lock_abandoned"
                message = f"{trigger} runtime global lock cleanup abandoned after retry budget: {exc}"
            else:
                mark_runtime_lock_resource_cleanup_failed(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    resource_id=resource.resource_id,
                    failed_at=timestamp,
                    cleanup_error=str(exc),
                )
                event_type = f"{trigger}_runtime_lock_warning"
                message = f"{trigger} runtime global lock cleanup failed: {exc}"
        else:
            mark_runtime_lock_resource_cleaned(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                resource_id=resource.resource_id,
                cleaned_at=timestamp,
            )
            event_type = f"{trigger}_runtime_lock_completed"
            message = f"{trigger} runtime global lock cleanup completed"
        record_runtime_lock_event(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=resource.resource_id,
            event_type=event_type,
            message=message,
            created_at=timestamp,
        )
        recovered_count += 1
    return recovered_count


def _resource_is_live(resource: RuntimeLockResource) -> bool:
    record = read_runtime_lock_record(lock_path=Path(resource.lock_path))
    if record is None:
        return False
    if not _record_matches_resource(record=record, resource=resource):
        return False
    return runtime_process_exists(record.owner_pid)


def _cleanup_runtime_lock_resource(resource: RuntimeLockResource) -> None:
    lock_path = Path(resource.lock_path)
    record = read_runtime_lock_record(lock_path=lock_path)
    if record is None:
        if not lock_path.exists():
            return
        raise RuntimeError("runtime global lock owner is unreadable")
    if not _record_matches_resource(record=record, resource=resource):
        return
    if runtime_process_exists(record.owner_pid):
        raise RuntimeError("runtime global lock is still held by a live owner")
    lock_path.unlink()


def _record_matches_resource(
    *,
    record: RuntimeLockRecord,
    resource: RuntimeLockResource,
) -> bool:
    return record.owner_pid == resource.owner_pid and record.lock_id == resource.lock_id


__all__ = [
    "RuntimeProcessLockAcquisition",
    "RuntimeLockLeaseAttachment",
    "RuntimeLockReleaseResult",
    "RuntimeProcessLockLease",
    "acquire_runtime_lock_for_startup",
    "attach_runtime_lock_lease",
    "reconcile_runtime_lock_resources_for_periodic_reaper",
    "reconcile_runtime_lock_resources_for_startup",
    "release_runtime_lock_lease",
]

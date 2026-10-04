from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from .cancel_cleanup_persistence import (
    CleanupPersistenceError,
    persist_action_cleanup_failure,
    persist_action_cleanup_success,
)
from .resource_cleanup import cleanup_runtime_resource
from .resource_repository import list_recoverable_tool_runtime_resources_for_action


@dataclass(frozen=True, slots=True)
class ActionCleanupPassResult:
    cleaned_count: int
    affected_resource_count: int
    execution_failure_count: int
    persistence_failure_count: int
    failed_resource_kinds: tuple[str, ...]
    abandoned_count: int

    @property
    def failure_count(self) -> int:
        return self.affected_resource_count

    @property
    def warning_message(self) -> str | None:
        if self.failure_count == 0:
            return None
        kinds = (
            ", ".join(self.failed_resource_kinds)
            if self.failed_resource_kinds
            else "unknown"
        )
        if self.execution_failure_count == 0 and self.persistence_failure_count > 0:
            return (
                f"failed to persist cleanup results for {self.failure_count} "
                "post-terminal action resources "
                f"(kinds: {kinds}; persistence_failures={self.persistence_failure_count})"
            )
        count_summary = (
            f"execution_failures={self.execution_failure_count}; "
            f"persistence_failures={self.persistence_failure_count}"
        )
        abandoned_suffix = (
            f"; abandoned={self.abandoned_count}" if self.abandoned_count > 0 else ""
        )
        return (
            f"failed to clean up {self.failure_count} post-terminal action resources "
            f"(kinds: {kinds}; {count_summary}{abandoned_suffix})"
        )


async def cancel_action_runtime_resources(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    action_id: str,
    execution_session_id: str,
) -> ActionCleanupPassResult:
    return await asyncio.to_thread(
        cleanup_action_runtime_resources,
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        action_id=action_id,
        execution_session_id=execution_session_id,
    )


def cleanup_action_runtime_resources(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    action_id: str,
    execution_session_id: str,
) -> ActionCleanupPassResult:
    resources = sorted(
        list_recoverable_tool_runtime_resources_for_action(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            action_id=action_id,
            execution_session_id=execution_session_id,
        ),
        key=lambda resource: (
            resource.resource_kind != "process_group",
            resource.created_at,
            resource.resource_id,
        ),
    )
    cleaned_count = 0
    affected_resource_count = 0
    execution_failure_count = 0
    persistence_failure_count = 0
    failed_resource_kinds: list[str] = []
    abandoned_count = 0
    process_cleanup_failed = False
    for resource in resources:
        if process_cleanup_failed and resource.resource_kind != "process_group":
            break
        if resource.status == "abandoned":
            process_cleanup_failed = True
            affected_resource_count += 1
            failed_resource_kinds.append(resource.resource_kind)
            abandoned_count += 1
            continue
        timestamp = now_utc_iso()
        execution_failed = False
        persistence_failed = False
        cleanup_error: str | None = None
        resource_status_after_persistence = "unchanged"
        try:
            cleanup_runtime_resource(resource)
        except Exception as exc:  # noqa: BLE001
            execution_failed = True
            cleanup_error = str(exc)
        try:
            if execution_failed:
                if cleanup_error is None:
                    raise RuntimeError("cleanup error message is missing")
                persistence_result = persist_action_cleanup_failure(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    resource=resource,
                    failed_at=timestamp,
                    cleanup_error=cleanup_error,
                )
                resource_status_after_persistence = (
                    persistence_result.resource_status_after_persistence
                )
            else:
                persistence_result = persist_action_cleanup_success(
                    db_path=db_path,
                    busy_timeout_ms=busy_timeout_ms,
                    resource=resource,
                    cleaned_at=timestamp,
                )
                resource_status_after_persistence = (
                    persistence_result.resource_status_after_persistence
                )
        except CleanupPersistenceError as exc:
            persistence_failed = True
            resource_status_after_persistence = exc.resource_status_after_persistence
        if execution_failed:
            execution_failure_count += 1
            failed_resource_kinds.append(resource.resource_kind)
            if resource.resource_kind == "process_group":
                process_cleanup_failed = True
        if persistence_failed:
            persistence_failure_count += 1
            if resource.resource_kind not in failed_resource_kinds:
                failed_resource_kinds.append(resource.resource_kind)
        if resource_status_after_persistence == "abandoned":
            abandoned_count += 1
        if execution_failed or persistence_failed:
            affected_resource_count += 1
        if not execution_failed and not persistence_failed:
            cleaned_count += 1
    return ActionCleanupPassResult(
        cleaned_count=cleaned_count,
        affected_resource_count=affected_resource_count,
        execution_failure_count=execution_failure_count,
        persistence_failure_count=persistence_failure_count,
        failed_resource_kinds=tuple(sorted(set(failed_resource_kinds))),
        abandoned_count=abandoned_count,
    )


__all__ = [
    "ActionCleanupPassResult",
    "cancel_action_runtime_resources",
    "cleanup_action_runtime_resources",
]

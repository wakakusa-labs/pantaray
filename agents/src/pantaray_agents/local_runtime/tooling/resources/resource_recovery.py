from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.schema.agent.base import JSONValue

from ..models import (
    StoredInflightToolInvocation,
    StoredToolRuntimeResource,
    ToolRuntimeResourceStatus,
)
from ..repository.executions import ToolInvocationTerminalStateError
from ..tool_result_finalization import (
    InvocationToolResultOwner,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)
from .action_session_temp_authority import (
    load_action_session_temp_cleanup_receipt,
)
from .action_session_temp_cleanup import cleanup_action_session_temp
from .resource_cleanup import (
    cleanup_runtime_resource,
    resource_cleanup_attempts_exhausted,
)
from .resource_repository import (
    list_inflight_tool_invocations,
    list_periodic_recoverable_tool_runtime_resources,
    list_recoverable_tool_runtime_resources,
    list_terminal_action_session_temp_cleanup_candidates,
)
from .resource_transition_store import (
    ToolRuntimeResourceEventInput,
    ToolRuntimeResourceTransitionStatus,
    persist_tool_runtime_resource_transition,
)

RecoveryTrigger = Literal["startup", "periodic"]
RecoveryScope = Literal["inflight", "lingering"]
STARTUP_RECOVERY_ERROR_TYPE = "StartupRecoveryInterruptedToolInvocation"
PERIODIC_REAPER_ERROR_TYPE = "PeriodicReaperInterruptedToolInvocation"


def _build_startup_recovery_warning(
    *,
    trigger: RecoveryTrigger,
    resource_id: str | None,
    resource_kind: str | None,
    message: str,
) -> dict[str, JSONValue]:
    warning: dict[str, JSONValue] = {
        "type": _warning_type(trigger),
        "message": message,
    }
    if resource_id is not None:
        warning["resource_id"] = resource_id
    if resource_kind is not None:
        warning["resource_kind"] = resource_kind
    return warning


def _build_startup_recovery_output(
    *,
    trigger: RecoveryTrigger,
    warnings: tuple[dict[str, JSONValue], ...],
) -> dict[str, JSONValue]:
    return {
        "error": {
            "type": _recovery_error_type(trigger),
            "message": _recovery_error_message(trigger),
        },
        "warnings": list(warnings),
    }


def _group_resources_by_session(
    resources: tuple[StoredToolRuntimeResource, ...],
) -> dict[str, list[StoredToolRuntimeResource]]:
    grouped: dict[str, list[StoredToolRuntimeResource]] = defaultdict(list)
    for resource in resources:
        grouped[resource.execution_session_id].append(resource)
    return grouped


def _recover_action_session_temp_roots(
    *,
    db_path: Path,
    busy_timeout_ms: int,
) -> int:
    recovered_count = 0
    candidates = list_terminal_action_session_temp_cleanup_candidates(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    for candidate in candidates:
        receipt = load_action_session_temp_cleanup_receipt(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=candidate.user_id,
            action_id=candidate.action_id,
            execution_session_id=candidate.execution_session_id,
        )
        if receipt is None:
            continue
        cleanup_action_session_temp(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            receipt=receipt,
        )
        recovered_count += 1
    return recovered_count


def _recover_resources(
    *,
    trigger: RecoveryTrigger,
    db_path: Path,
    busy_timeout_ms: int,
    resources: tuple[StoredToolRuntimeResource, ...],
    inflight_invocation_ids: set[str],
    inflight_action_ids: set[str],
) -> tuple[int, tuple[tuple[str, dict[str, JSONValue]], ...]]:
    invocation_warnings: list[tuple[str, dict[str, JSONValue]]] = []
    recovered_count = 0
    process_cleanup_blocker: dict[str, JSONValue] | None = None
    ordered_resources = sorted(
        resources,
        key=lambda resource: (
            resource.resource_kind != "process_group",
            resource.created_at,
            resource.resource_id,
        ),
    )
    for resource in ordered_resources:
        if (
            process_cleanup_blocker is not None
            and resource.resource_kind != "process_group"
        ):
            warned_invocation_ids = {
                invocation_id
                for invocation_id, warning in invocation_warnings
                if warning is process_cleanup_blocker
            }
            for blocked_resource in ordered_resources:
                invocation_id = blocked_resource.tool_invocation_id
                if (
                    blocked_resource.resource_kind != "process_group"
                    and invocation_id is not None
                    and invocation_id not in warned_invocation_ids
                ):
                    invocation_warnings.append((invocation_id, process_cleanup_blocker))
                    warned_invocation_ids.add(invocation_id)
            break
        if resource.status == "abandoned":
            warning = _build_startup_recovery_warning(
                trigger=trigger,
                resource_id=resource.resource_id,
                resource_kind=resource.resource_kind,
                message="cleanup was already abandoned by prior recovery",
            )
            if process_cleanup_blocker is None:
                process_cleanup_blocker = warning
            if resource.tool_invocation_id is not None:
                invocation_warnings.append((resource.tool_invocation_id, warning))
            continue
        recovered_count += 1
        timestamp = now_utc_iso()
        cleanup_error: str | None
        try:
            cleanup_runtime_resource(resource)
        except Exception as exc:
            if resource_cleanup_attempts_exhausted(resource):
                status: ToolRuntimeResourceTransitionStatus = "abandoned"
                message = f"{_cleanup_prefix(trigger)} cleanup abandoned after retry budget: {exc}"
            else:
                status = "cleanup_failed"
                message = f"{_cleanup_prefix(trigger)} cleanup failed: {exc}"
            cleanup_error = str(exc)
        else:
            status = "cleaned"
            cleanup_error = None
            message = f"{_cleanup_prefix(trigger)} cleanup completed"
        scope: RecoveryScope = "lingering"
        if resource.tool_invocation_id in inflight_invocation_ids or (
            resource.tool_invocation_id is None
            and resource.action_id in inflight_action_ids
        ):
            scope = "inflight"
        transition = persist_tool_runtime_resource_transition(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource=resource,
            status=status,
            timestamp=timestamp,
            cleanup_error=cleanup_error,
            event=ToolRuntimeResourceEventInput(
                event_type=_resource_event_type(trigger, scope, message),
                message=f"{resource.resource_kind}: {message}",
                tool_invocation_id=resource.tool_invocation_id,
            ),
        )
        if not transition.applied:
            message = _concurrent_transition_message(
                status=transition.current_resource.status,
            )
        warning = _build_startup_recovery_warning(
            trigger=trigger,
            resource_id=resource.resource_id,
            resource_kind=resource.resource_kind,
            message=message,
        )
        if (
            process_cleanup_blocker is None
            and resource.resource_kind == "process_group"
            and transition.current_resource.status != "cleaned"
        ):
            process_cleanup_blocker = warning
        if resource.tool_invocation_id is not None:
            invocation_warnings.append((resource.tool_invocation_id, warning))
    return recovered_count, tuple(invocation_warnings)


def _concurrent_transition_message(*, status: ToolRuntimeResourceStatus) -> str:
    if status == "cleaned":
        return "cleanup completed concurrently"
    if status == "abandoned":
        return "cleanup was already abandoned by concurrent recovery"
    return "cleanup state changed concurrently; recovery remains pending"


def _recover_invocation_without_resources(
    *,
    trigger: RecoveryTrigger,
    invocation: StoredInflightToolInvocation,
) -> tuple[int, tuple[dict[str, JSONValue], ...]]:
    return (
        0,
        (
            _build_startup_recovery_warning(
                trigger=trigger,
                resource_id=None,
                resource_kind=None,
                message=(
                    f"{_cleanup_prefix(trigger)} recovery found inflight tool invocation without tracked resources"
                ),
            ),
        ),
    )


def _cleanup_prefix(trigger: RecoveryTrigger) -> str:
    if trigger == "startup":
        return "startup"
    return "periodic reaper"


def _warning_type(trigger: RecoveryTrigger) -> str:
    if trigger == "startup":
        return "startup_recovery_cleanup"
    return "periodic_reaper_cleanup"


def _recovery_error_type(trigger: RecoveryTrigger) -> str:
    if trigger == "startup":
        return STARTUP_RECOVERY_ERROR_TYPE
    return PERIODIC_REAPER_ERROR_TYPE


def _recovery_error_message(trigger: RecoveryTrigger) -> str:
    if trigger == "startup":
        return "tool invocation was interrupted and recovered during startup"
    return "tool invocation was interrupted and recovered by periodic reaper"


def _resource_event_type(
    trigger: RecoveryTrigger,
    scope: RecoveryScope,
    message: str,
) -> str:
    if trigger == "startup":
        prefix = "startup_cleanup"
    else:
        prefix = "periodic_cleanup"
    scope_prefix = "inflight" if scope == "inflight" else "lingering"
    if "cleanup abandoned" in message:
        return f"{prefix}_{scope_prefix}_abandoned"
    if "cleanup completed" in message:
        return f"{prefix}_{scope_prefix}_completed"
    return f"{prefix}_{scope_prefix}_warning"


def _reconcile_tool_runtime_resources(
    *,
    trigger: RecoveryTrigger,
    db_path: Path,
    busy_timeout_ms: int,
) -> int:
    if trigger == "startup":
        inflight_invocations = list_inflight_tool_invocations(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
        )
        resources = list_recoverable_tool_runtime_resources(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
        )
    else:
        inflight_invocations = ()
        resources = list_periodic_recoverable_tool_runtime_resources(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
        )
    inflight_invocation_ids = {
        invocation.invocation_id for invocation in inflight_invocations
    }
    inflight_action_ids = {invocation.action_id for invocation in inflight_invocations}
    resource_invocation_ids = {
        resource.tool_invocation_id
        for resource in resources
        if resource.tool_invocation_id is not None
    }
    warnings_by_invocation: dict[str, list[dict[str, JSONValue]]] = defaultdict(list)
    resource_count = 0
    for session_resources in _group_resources_by_session(resources).values():
        recovered_count, invocation_warnings = _recover_resources(
            trigger=trigger,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resources=tuple(session_resources),
            inflight_invocation_ids=inflight_invocation_ids,
            inflight_action_ids=inflight_action_ids,
        )
        resource_count += recovered_count
        for invocation_id, warning in invocation_warnings:
            warnings_by_invocation[invocation_id].append(warning)
    completed_at = now_utc_iso()
    for invocation in inflight_invocations:
        if invocation.invocation_id in resource_invocation_ids:
            warnings = tuple(warnings_by_invocation[invocation.invocation_id])
        else:
            _, warnings = _recover_invocation_without_resources(
                trigger=trigger,
                invocation=invocation,
            )
        output_json = _build_startup_recovery_output(
            trigger=trigger,
            warnings=warnings,
        )
        try:
            finalize_local_tool_result(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                request=ToolResultFinalizationRequest(
                    owner=InvocationToolResultOwner(
                        invocation_id=invocation.invocation_id,
                        completed_at=completed_at,
                        status="failed",
                        completion_scope="invocation",
                    ),
                    output=output_json,
                ),
            )
        except ToolInvocationTerminalStateError:
            # A concurrent terminal writer won; its result is now the source of truth.
            continue
    resource_count += _recover_action_session_temp_roots(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )
    return resource_count


def reconcile_tool_runtime_resources_for_startup(
    *,
    db_path: Path,
    busy_timeout_ms: int,
) -> int:
    return _reconcile_tool_runtime_resources(
        trigger="startup",
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )


def reconcile_tool_runtime_resources_for_periodic_reaper(
    *,
    db_path: Path,
    busy_timeout_ms: int,
) -> int:
    return _reconcile_tool_runtime_resources(
        trigger="periodic",
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
    )


__all__ = [
    "reconcile_tool_runtime_resources_for_periodic_reaper",
    "reconcile_tool_runtime_resources_for_startup",
]

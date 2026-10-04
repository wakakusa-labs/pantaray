from __future__ import annotations

import os
import shutil
from pathlib import Path

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from ...descriptor_access import (
    DescriptorPathError,
    DescriptorPathMissingError,
    open_directory_descriptor,
)
from ..action_session_temp_paths import validate_action_storage_component
from ..models import StoredToolRuntimeResource
from .action_session_temp_authority import ActionSessionTempCleanupReceipt
from .resource_cleanup import resource_cleanup_attempts_exhausted
from .resource_store import load_tool_runtime_resource
from .resource_transition_store import (
    ToolRuntimeResourceEventInput,
    ToolRuntimeResourceTransitionOutcome,
    ToolRuntimeResourceTransitionStatus,
    persist_tool_runtime_resource_transition,
)


class ActionSessionTempCleanupAuthorityError(RuntimeError):
    """The current root resource no longer matches its cleanup receipt."""


class DescriptorSafeRemovalUnavailableError(RuntimeError):
    """The platform cannot remove a directory through a parent descriptor."""


def cleanup_action_session_temp(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    receipt: ActionSessionTempCleanupReceipt,
) -> ToolRuntimeResourceTransitionOutcome:
    resource = load_tool_runtime_resource(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        resource_id=receipt.root_resource_id,
    )
    try:
        remove_action_session_temp_leaf(resource=resource, receipt=receipt)
    except (OSError, DescriptorPathError, DescriptorSafeRemovalUnavailableError) as exc:
        cleanup_error = str(exc) or type(exc).__name__
        retry_exhausted = resource_cleanup_attempts_exhausted(resource)
        return _persist_cleanup_outcome(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource=resource,
            status="abandoned" if retry_exhausted else "cleanup_failed",
            cleanup_error=cleanup_error,
            message=(
                f"session temp cleanup abandoned after retry budget: {cleanup_error}"
                if retry_exhausted
                else f"session temp cleanup failed: {cleanup_error}"
            ),
        )
    return _persist_cleanup_outcome(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        resource=resource,
        status="cleaned",
        cleanup_error=None,
        message="session temp cleanup completed",
    )


def remove_action_session_temp_leaf(
    *,
    resource: StoredToolRuntimeResource,
    receipt: ActionSessionTempCleanupReceipt,
) -> None:
    """Delete one receipt-bound leaf without changing durable resource state."""

    _require_current_authority(resource=resource, receipt=receipt)
    _remove_session_leaf(receipt)


def _require_current_authority(
    *,
    resource: StoredToolRuntimeResource,
    receipt: ActionSessionTempCleanupReceipt,
) -> None:
    validate_action_storage_component(
        field_name="execution_session_id",
        value=receipt.execution_session_id,
    )
    if (
        not receipt.canonical_leaf.is_absolute()
        or receipt.canonical_leaf.parent / receipt.execution_session_id
        != receipt.canonical_leaf
        or resource.execution_session_id != receipt.execution_session_id
        or resource.tool_invocation_id is not None
        or resource.action_id != receipt.action_id
        or resource.resource_kind != "temp_dir"
        or resource.status != receipt.root_resource_status
        or resource.resource_path != str(receipt.canonical_leaf)
        or resource.updated_at != receipt.root_resource_updated_at
        or resource.cleanup_attempts != receipt.root_cleanup_attempts
    ):
        raise ActionSessionTempCleanupAuthorityError(
            "session temp cleanup receipt no longer matches the root resource"
        )


def _remove_session_leaf(receipt: ActionSessionTempCleanupReceipt) -> None:
    remove_descriptor_confined_directory_child(
        parent_path=receipt.canonical_leaf.parent,
        child_name=receipt.execution_session_id,
    )


def remove_descriptor_confined_directory_child(
    *, parent_path: Path, child_name: str
) -> None:
    """Delete one validated child through its canonical parent descriptor."""

    validate_action_storage_component(field_name="directory child", value=child_name)
    if not shutil.rmtree.avoids_symlink_attacks:
        raise DescriptorSafeRemovalUnavailableError(
            "fd-safe directory removal is unavailable"
        )
    try:
        parent_descriptor = open_directory_descriptor(root_path=parent_path)
    except DescriptorPathMissingError:
        return
    try:
        try:
            shutil.rmtree(
                child_name,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            try:
                os.stat(
                    child_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _persist_cleanup_outcome(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    resource: StoredToolRuntimeResource,
    status: ToolRuntimeResourceTransitionStatus,
    cleanup_error: str | None,
    message: str,
) -> ToolRuntimeResourceTransitionOutcome:
    return persist_tool_runtime_resource_transition(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        resource=resource,
        status=status,
        timestamp=now_utc_iso(),
        cleanup_error=cleanup_error,
        event=ToolRuntimeResourceEventInput(
            event_type=f"action_session_temp_{status}",
            message=message,
            tool_invocation_id=None,
        ),
    )


__all__ = [
    "ActionSessionTempCleanupAuthorityError",
    "DescriptorSafeRemovalUnavailableError",
    "cleanup_action_session_temp",
    "remove_action_session_temp_leaf",
    "remove_descriptor_confined_directory_child",
]

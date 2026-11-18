from __future__ import annotations

import errno
import os
import shutil
import signal
import time
from pathlib import Path

from ..locks.workspace_lock import remove_workspace_lock_file_if_owned
from ..models import StoredToolRuntimeResource
from .process_identity import classify_process_identity

RESOURCE_CLEANUP_POLL_INTERVAL_SECONDS = 0.05
RESOURCE_CLEANUP_POLL_ATTEMPTS = 20
MAX_RESOURCE_CLEANUP_ATTEMPTS = 3


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resource_cleanup_attempts_exhausted(resource: StoredToolRuntimeResource) -> bool:
    return resource.cleanup_attempts + 1 >= MAX_RESOURCE_CLEANUP_ATTEMPTS


def cleanup_runtime_resource(resource: StoredToolRuntimeResource) -> None:
    if resource.resource_kind == "process_group":
        _cleanup_process_group(resource)
        return
    if resource.resource_kind == "lock":
        _cleanup_workspace_lock(resource)
        return
    if resource.resource_kind in {"temp_file", "temp_dir"}:
        _cleanup_path_resource(resource)
        return
    raise RuntimeError(f"unsupported resource kind: {resource.resource_kind}")


def _cleanup_process_group(resource: StoredToolRuntimeResource) -> None:
    if resource.pid is None:
        raise RuntimeError("process_group resource requires pid")
    identity_status = classify_process_identity(
        pid=resource.pid,
        expected_start_signature=resource.process_start_signature,
    )
    if identity_status == "unknown":
        raise RuntimeError("process identity could not be confirmed during cleanup")
    if identity_status == "mismatch_or_absent":
        # Design limit: only the recorded leader's start signature proves the
        # group is ours, so a group whose leader (the sandbox helper) was killed
        # by SIGKILL while descendants live is not reached here; signalling the
        # pgid unverified could hit an unrelated group after pid reuse. Revisit
        # if groups are observed leaking after a helper's death.
        return
    if resource.pgid is None:
        raise RuntimeError("live process_group resource requires pgid")
    try:
        os.killpg(resource.pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise
    for _ in range(RESOURCE_CLEANUP_POLL_ATTEMPTS):
        if not process_exists(resource.pid):
            return
        time.sleep(RESOURCE_CLEANUP_POLL_INTERVAL_SECONDS)
    raise RuntimeError("process group still exists after cleanup")


def _cleanup_path_resource(resource: StoredToolRuntimeResource) -> None:
    if resource.resource_path is None:
        raise RuntimeError(f"{resource.resource_kind} resource requires resource_path")
    target_path = Path(resource.resource_path)
    if not target_path.exists():
        return
    if resource.resource_kind == "temp_dir":
        shutil.rmtree(target_path)
        return
    if target_path.is_dir():
        raise RuntimeError(
            f"{resource.resource_kind} resource path unexpectedly points to a directory"
        )
    target_path.unlink()


def _cleanup_workspace_lock(resource: StoredToolRuntimeResource) -> None:
    if resource.resource_path is None:
        raise RuntimeError("lock resource requires resource_path")
    if resource.tool_invocation_id is None:
        raise RuntimeError("lock resource requires tool_invocation_id")
    if resource.lock_id is None:
        raise RuntimeError("lock resource requires lock_id")
    remove_workspace_lock_file_if_owned(
        lock_path=Path(resource.resource_path),
        tool_invocation_id=resource.tool_invocation_id,
        lock_id=resource.lock_id,
    )


__all__ = [
    "MAX_RESOURCE_CLEANUP_ATTEMPTS",
    "cleanup_runtime_resource",
    "process_exists",
    "resource_cleanup_attempts_exhausted",
]

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from pantaray_agents.schema.memory_run_workspace import (
    MEMORY_RUN_TOOL_RESULTS_DIRNAME,
    MemoryRunWorkspace,
)

MEMORY_CATALOG_DIRNAME = "memory_catalog"
MEMORY_USERS_DIRNAME = "users"
MEMORY_RUN_WORKSPACES_DIRNAME = "workspaces"
_PRIVATE_DIRECTORY_MODE = 0o700
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_EPHEMERAL_CLEANUP_MAX_ATTEMPTS = 3
_EPHEMERAL_CLEANUP_RETRY_DELAY_SECONDS = 0.05

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryRunWorkspaceCleanupResult:
    removed_count: int
    failed_count: int


class MemoryRunWorkspaceScope:
    """Own one run workspace and its verified filesystem deletion authority."""

    def __init__(self) -> None:
        self._entered = False
        self._closed = False
        self._workspaces_fd: int | None = None
        self._workspace_name: str | None = None
        self._workspace: MemoryRunWorkspace | None = None

    def __enter__(self) -> MemoryRunWorkspaceScope:
        if self._entered:
            raise RuntimeError("memory run workspace scope cannot be reused")
        _require_fd_safe_rmtree()
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def create(
        self,
        *,
        artifact_root: Path,
        user_id: str,
        run_id: str,
    ) -> MemoryRunWorkspace:
        if not self._entered or self._closed:
            raise RuntimeError("memory run workspace scope is not active")
        if self._workspace_name is not None:
            raise RuntimeError("memory run workspace scope already owns a workspace")
        for value in (user_id, run_id):
            if not _is_path_component(value):
                raise ValueError("memory run workspace identity is invalid")

        absolute_artifact_root = artifact_root.absolute()
        workspace_name = f"{run_id}-{uuid.uuid4().hex}"
        root_path = (
            memory_run_workspaces_path(
                artifact_root=absolute_artifact_root,
                user_id=user_id,
            )
            / workspace_name
        )
        workspaces_fd = _open_workspaces_directory(
            artifact_root=absolute_artifact_root,
            user_id=user_id,
        )
        self._workspaces_fd = workspaces_fd
        self._workspace_name = workspace_name
        _materialize_workspace(
            workspaces_fd=workspaces_fd,
            workspace_name=workspace_name,
        )
        workspace = MemoryRunWorkspace.from_root_path(root_path)
        self._workspace = workspace
        return workspace

    def require_tool_results_path(self) -> Path:
        if self._closed or self._workspace is None:
            raise RuntimeError("memory run workspace has not been created")
        return self._workspace.tool_results_path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        workspaces_fd = self._workspaces_fd
        workspace_name = self._workspace_name
        try:
            if workspaces_fd is not None and workspace_name is not None:
                _remove_workspace_tree_with_retry(
                    workspaces_fd=workspaces_fd,
                    workspace_name=workspace_name,
                )
        finally:
            if workspaces_fd is not None:
                os.close(workspaces_fd)


def cleanup_orphaned_memory_run_workspaces(
    *, artifact_root: Path
) -> MemoryRunWorkspaceCleanupResult:
    """Remove only real workspace directories below verified catalog directories."""

    _require_fd_safe_rmtree()
    absolute_artifact_root = artifact_root.absolute()
    root_fd = os.open(absolute_artifact_root, _DIRECTORY_OPEN_FLAGS)
    try:
        catalog_fd = _open_existing_directory(
            parent_fd=root_fd,
            name=MEMORY_CATALOG_DIRNAME,
        )
        if catalog_fd is None:
            return MemoryRunWorkspaceCleanupResult(0, 0)
        try:
            users_fd = _open_existing_directory(
                parent_fd=catalog_fd,
                name=MEMORY_USERS_DIRNAME,
            )
            if users_fd is None:
                return MemoryRunWorkspaceCleanupResult(0, 0)
            try:
                return _cleanup_user_workspaces(users_fd=users_fd)
            finally:
                os.close(users_fd)
        finally:
            os.close(catalog_fd)
    finally:
        os.close(root_fd)


def memory_run_workspaces_path(*, artifact_root: Path, user_id: str) -> Path:
    if not _is_path_component(user_id):
        raise ValueError("memory run workspace user identity is invalid")
    return (
        artifact_root
        / MEMORY_CATALOG_DIRNAME
        / MEMORY_USERS_DIRNAME
        / user_id
        / MEMORY_RUN_WORKSPACES_DIRNAME
    )


def _open_workspaces_directory(*, artifact_root: Path, user_id: str) -> int:
    artifact_root.mkdir(
        parents=True,
        mode=_PRIVATE_DIRECTORY_MODE,
        exist_ok=True,
    )
    root_fd = os.open(artifact_root, _DIRECTORY_OPEN_FLAGS)
    opened_fds: list[int] = []
    try:
        parent_fd = root_fd
        for name in (
            MEMORY_CATALOG_DIRNAME,
            MEMORY_USERS_DIRNAME,
            user_id,
            MEMORY_RUN_WORKSPACES_DIRNAME,
        ):
            child_fd = _open_or_create_directory(parent_fd=parent_fd, name=name)
            opened_fds.append(child_fd)
            parent_fd = child_fd
        return opened_fds.pop()
    finally:
        for opened_fd in reversed(opened_fds):
            os.close(opened_fd)
        os.close(root_fd)


def _open_or_create_directory(*, parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=_PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return _open_required_directory(parent_fd=parent_fd, name=name)


def _open_required_directory(*, parent_fd: int, name: str) -> int:
    return os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)


def _open_existing_directory(*, parent_fd: int, name: str) -> int | None:
    try:
        return _open_required_directory(parent_fd=parent_fd, name=name)
    except FileNotFoundError:
        return None


def _materialize_workspace(*, workspaces_fd: int, workspace_name: str) -> None:
    os.mkdir(
        workspace_name,
        mode=_PRIVATE_DIRECTORY_MODE,
        dir_fd=workspaces_fd,
    )
    os.fsync(workspaces_fd)
    workspace_fd = _open_required_directory(
        parent_fd=workspaces_fd,
        name=workspace_name,
    )
    try:
        os.mkdir(
            MEMORY_RUN_TOOL_RESULTS_DIRNAME,
            mode=_PRIVATE_DIRECTORY_MODE,
            dir_fd=workspace_fd,
        )
        os.fsync(workspace_fd)
    finally:
        os.close(workspace_fd)


def _cleanup_user_workspaces(*, users_fd: int) -> MemoryRunWorkspaceCleanupResult:
    removed_count = 0
    failed_count = 0
    with os.scandir(users_fd) as user_entries:
        for user_entry in user_entries:
            if not user_entry.is_dir(follow_symlinks=False):
                failed_count += 1
                continue
            try:
                user_fd = _open_required_directory(
                    parent_fd=users_fd,
                    name=user_entry.name,
                )
            except OSError:
                failed_count += 1
                continue
            try:
                workspaces_fd = _open_existing_directory(
                    parent_fd=user_fd,
                    name=MEMORY_RUN_WORKSPACES_DIRNAME,
                )
                if workspaces_fd is None:
                    continue
                try:
                    removed, failed = _cleanup_workspace_entries(
                        workspaces_fd=workspaces_fd
                    )
                    removed_count += removed
                    failed_count += failed
                finally:
                    os.close(workspaces_fd)
            except OSError:
                failed_count += 1
            finally:
                os.close(user_fd)
    return MemoryRunWorkspaceCleanupResult(removed_count, failed_count)


def _cleanup_workspace_entries(*, workspaces_fd: int) -> tuple[int, int]:
    removed_count = 0
    failed_count = 0
    with os.scandir(workspaces_fd) as workspace_entries:
        for workspace_entry in workspace_entries:
            if not workspace_entry.is_dir(follow_symlinks=False):
                failed_count += 1
                continue
            if _remove_workspace_tree(
                workspaces_fd=workspaces_fd,
                workspace_name=workspace_entry.name,
                operation="startup_orphaned_memory_run_workspace",
                log_failure=True,
            ):
                removed_count += 1
            else:
                failed_count += 1
    return removed_count, failed_count


def _remove_workspace_tree(
    *, workspaces_fd: int, workspace_name: str, operation: str, log_failure: bool
) -> bool:
    try:
        shutil.rmtree(workspace_name, dir_fd=workspaces_fd)
    except FileNotFoundError:
        return True
    except OSError:
        if log_failure:
            logger.exception("Failed ephemeral cleanup: operation=%s", operation)
        return False
    return True


def _remove_workspace_tree_with_retry(
    *, workspaces_fd: int, workspace_name: str
) -> None:
    # Design limit: cleanup is retried only in-process. If "Failed ephemeral cleanup"
    # appears in operational logs, promote deletion to the memory_artifact_deletions ledger.
    for attempt in range(_EPHEMERAL_CLEANUP_MAX_ATTEMPTS):
        if _remove_workspace_tree(
            workspaces_fd=workspaces_fd,
            workspace_name=workspace_name,
            operation="memory_run_workspace",
            log_failure=False,
        ):
            return
        if attempt < _EPHEMERAL_CLEANUP_MAX_ATTEMPTS - 1:
            time.sleep(_EPHEMERAL_CLEANUP_RETRY_DELAY_SECONDS)
    logger.error(
        "Failed ephemeral cleanup: operation=memory_run_workspace workspace=%s",
        workspace_name,
    )


def _require_fd_safe_rmtree() -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimeError(
            "fd-safe recursive removal is required for Memory workspaces"
        )


def _is_path_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "\\" not in value
        and "\0" not in value
    )


__all__ = [
    "MEMORY_CATALOG_DIRNAME",
    "MEMORY_RUN_WORKSPACES_DIRNAME",
    "MEMORY_USERS_DIRNAME",
    "MemoryRunWorkspaceCleanupResult",
    "MemoryRunWorkspaceScope",
    "cleanup_orphaned_memory_run_workspaces",
    "memory_run_workspaces_path",
]

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .tool_result_storage import ACTION_TOOL_RESULTS_DIRNAME

LOCAL_RUNTIME_WORKSPACE_DIRNAME = "local_runtime_workspaces"
SCRATCH_WORKSPACE_DIRNAME = "scratch"
SCRATCH_SESSION_TEMP_DIRNAME = ".runtime-temp"
PRIVATE_TEMP_DIRNAME = "temp"
MANAGED_DIRECTORY_MODE = 0o700


class ActionSessionTempPathError(ValueError):
    """An Action session-temp identity cannot form a managed path."""


@dataclass(frozen=True, slots=True)
class ActionStoragePaths:
    storage_base: Path
    action_root: Path
    workspace: Path
    tool_results: Path
    session_temp_root: Path


def resolve_local_runtime_storage_base(*, db_path: Path) -> Path:
    """Return the canonical directory that owns local-runtime sidecar storage."""

    return db_path.resolve().parent


def create_private_temp_dir(*, db_path: Path, prefix: str) -> Path:
    """A fresh directory for one command or conversion, inside private app storage.

    Sandboxed commands may read and write the OS temp dirs, so what Pantaray
    keeps there for one of them would be open to every other command.
    """

    # Design limit: a crash leaves the dirs of unrecorded commands and office
    # conversions behind; sweep this root at startup if it is seen to grow.
    root = resolve_local_runtime_storage_base(db_path=db_path) / PRIVATE_TEMP_DIRNAME
    root.mkdir(mode=MANAGED_DIRECTORY_MODE, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root)).resolve()


def resolve_managed_action_workspace_root(*, db_path: Path) -> Path:
    """Return the canonical root scanned by Action storage recovery."""

    return _managed_action_workspace_root(
        storage_base=resolve_local_runtime_storage_base(db_path=db_path)
    )


def resolve_action_storage_paths(
    *, db_path: Path, user_id: str, action_id: str
) -> ActionStoragePaths:
    validate_action_storage_component(field_name="user_id", value=user_id)
    validate_action_storage_component(field_name="action_id", value=action_id)
    storage_base = resolve_local_runtime_storage_base(db_path=db_path)
    action_root = (
        _managed_action_workspace_root(storage_base=storage_base) / user_id / action_id
    )
    workspace = action_root / SCRATCH_WORKSPACE_DIRNAME
    return ActionStoragePaths(
        storage_base=storage_base,
        action_root=action_root,
        workspace=workspace,
        tool_results=action_root / ACTION_TOOL_RESULTS_DIRNAME,
        session_temp_root=workspace / SCRATCH_SESSION_TEMP_DIRNAME,
    )


def resolve_action_session_temp_leaf(
    *, paths: ActionStoragePaths, execution_session_id: str
) -> Path:
    validate_action_storage_component(
        field_name="execution_session_id", value=execution_session_id
    )
    return paths.session_temp_root / execution_session_id


def validate_action_storage_component(*, field_name: str, value: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or Path(value).name != value
        or "\\" in value
        or "\0" in value
    ):
        raise ActionSessionTempPathError(f"{field_name} must be a non-path identifier")


def open_durable_action_storage_child(*, parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=MANAGED_DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    os.fsync(parent_fd)
    child_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        if stat.S_IMODE(os.fstat(child_fd).st_mode) != MANAGED_DIRECTORY_MODE:
            os.fchmod(child_fd, MANAGED_DIRECTORY_MODE)
            os.fsync(child_fd)
    except OSError:
        os.close(child_fd)
        raise
    return child_fd


def _managed_action_workspace_root(*, storage_base: Path) -> Path:
    return storage_base / LOCAL_RUNTIME_WORKSPACE_DIRNAME / SCRATCH_WORKSPACE_DIRNAME


__all__ = [
    "ActionSessionTempPathError",
    "ActionStoragePaths",
    "LOCAL_RUNTIME_WORKSPACE_DIRNAME",
    "MANAGED_DIRECTORY_MODE",
    "PRIVATE_TEMP_DIRNAME",
    "SCRATCH_SESSION_TEMP_DIRNAME",
    "SCRATCH_WORKSPACE_DIRNAME",
    "create_private_temp_dir",
    "open_durable_action_storage_child",
    "resolve_action_session_temp_leaf",
    "resolve_action_storage_paths",
    "resolve_local_runtime_storage_base",
    "resolve_managed_action_workspace_root",
    "validate_action_storage_component",
]

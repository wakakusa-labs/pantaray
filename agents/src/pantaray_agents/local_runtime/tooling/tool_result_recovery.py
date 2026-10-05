from __future__ import annotations

import logging
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.utils.structured_logging import log_structured_event

from .action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
    resolve_managed_action_workspace_root,
)
from .tool_result_recovery_references import (
    ToolResultRecoveryError,
    load_stored_tool_result_references,
)
from .tool_result_storage import (
    ACTION_TOOL_RESULTS_DIRNAME,
)

_FINAL_RESULT_FILE_PATTERN = re.compile(r"^output-[0-9a-f]{32}\.(?:bin|json)$")
_TEMP_RESULT_FILE_PATTERN = re.compile(r"^\.output-[0-9a-f]{32}\.tmp$")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolResultRecoveryResult:
    removed_file_count: int
    removed_directory_count: int
    preserved_file_count: int
    unknown_entry_count: int
    missing_reference_count: int


@dataclass(frozen=True, slots=True)
class _InvocationDirectory:
    workspace_root: Path
    user_name: str
    action_name: str
    tool_results_root: Path
    name: str
    managed_file_names: tuple[str, ...]
    unknown_entry_count: int


def reconcile_tool_results_for_startup(
    *, db_path: Path, busy_timeout_ms: int
) -> ToolResultRecoveryResult:
    """Remove unreferenced managed results; never delete a referenced one.

    A durable reference whose file is gone is counted and logged, not fatal:
    reading that one result fails later, while the rest of the app starts.
    """
    workspace_root = _workspace_root(db_path=db_path)
    references = load_stored_tool_result_references(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        workspace_root=workspace_root,
    )
    invocation_directories, unknown_entry_count = _scan_managed_storage(
        workspace_root=workspace_root
    )
    referenced_paths = {reference.path for reference in references}
    scanned_file_paths = {
        invocation.tool_results_root / invocation.name / file_name
        for invocation in invocation_directories
        for file_name in invocation.managed_file_names
    }
    missing_by_source_kind = Counter(
        reference.source_kind
        for reference in references
        if reference.path not in scanned_file_paths
    )
    missing_reference_count = missing_by_source_kind.total()
    if missing_reference_count:
        log_structured_event(
            logger,
            level="warning",
            evt="TOOL_RESULT_FILES_MISSING_AT_STARTUP",
            component="local_runtime.tooling.tool_result_recovery",
            missing_reference_count=missing_reference_count,
            missing_tool_invocation_reference_count=(
                missing_by_source_kind["tool_invocation"]
            ),
            missing_action_step_reference_count=missing_by_source_kind["action_step"],
        )

    removed_file_count = 0
    removed_directory_count = 0
    preserved_file_count = 0

    for invocation in invocation_directories:
        result = _reconcile_invocation_directory(
            invocation=invocation,
            referenced_paths=referenced_paths,
        )
        removed_file_count += result.removed_file_count
        removed_directory_count += result.removed_directory_count
        preserved_file_count += result.preserved_file_count

    return ToolResultRecoveryResult(
        removed_file_count=removed_file_count,
        removed_directory_count=removed_directory_count,
        preserved_file_count=preserved_file_count,
        unknown_entry_count=unknown_entry_count,
        missing_reference_count=missing_reference_count,
    )


@dataclass(frozen=True, slots=True)
class _InvocationRecoveryResult:
    removed_file_count: int
    removed_directory_count: int
    preserved_file_count: int


def _workspace_root(*, db_path: Path) -> Path:
    return resolve_managed_action_workspace_root(db_path=db_path)


def _scan_managed_storage(
    *, workspace_root: Path
) -> tuple[tuple[_InvocationDirectory, ...], int]:
    try:
        workspace_fd = os.open(
            workspace_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            _enforce_owner_only_directory(directory_fd=workspace_fd)
        except OSError:
            os.close(workspace_fd)
            raise
    except FileNotFoundError:
        return (), 0
    except OSError as exc:
        raise ToolResultRecoveryError(
            "failed to open the managed Action workspace root"
        ) from exc

    invocation_directories: list[_InvocationDirectory] = []
    unknown_entry_count = 0
    try:
        for user_entry in _directory_entries(workspace_fd):
            if not user_entry.is_dir(follow_symlinks=False):
                unknown_entry_count += 1
                continue
            user_fd = _open_child_directory(
                parent_fd=workspace_fd, name=user_entry.name
            )
            try:
                for action_entry in _directory_entries(user_fd):
                    if not action_entry.is_dir(follow_symlinks=False):
                        unknown_entry_count += 1
                        continue
                    action_fd = _open_child_directory(
                        parent_fd=user_fd, name=action_entry.name
                    )
                    try:
                        scanned, unknown = _scan_action_tool_results(
                            action_fd=action_fd,
                            workspace_root=workspace_root,
                            user_name=user_entry.name,
                            action_name=action_entry.name,
                            tool_results_root=(
                                workspace_root
                                / user_entry.name
                                / action_entry.name
                                / ACTION_TOOL_RESULTS_DIRNAME
                            ),
                        )
                        invocation_directories.extend(scanned)
                        unknown_entry_count += unknown
                    finally:
                        os.close(action_fd)
            finally:
                os.close(user_fd)
    except OSError as exc:
        raise ToolResultRecoveryError("failed to scan managed tool results") from exc
    finally:
        os.close(workspace_fd)
    return tuple(invocation_directories), unknown_entry_count


def _scan_action_tool_results(
    *,
    action_fd: int,
    workspace_root: Path,
    user_name: str,
    action_name: str,
    tool_results_root: Path,
) -> tuple[tuple[_InvocationDirectory, ...], int]:
    try:
        tool_results_fd = _open_child_directory(
            parent_fd=action_fd,
            name=ACTION_TOOL_RESULTS_DIRNAME,
        )
    except FileNotFoundError:
        return (), 0
    invocation_directories: list[_InvocationDirectory] = []
    unknown_entry_count = 0
    try:
        for invocation_entry in _directory_entries(tool_results_fd):
            if not invocation_entry.is_dir(follow_symlinks=False):
                unknown_entry_count += 1
                continue
            invocation_fd = _open_child_directory(
                parent_fd=tool_results_fd, name=invocation_entry.name
            )
            try:
                managed_file_names: list[str] = []
                invocation_unknown_count = 0
                for result_entry in _directory_entries(invocation_fd):
                    if result_entry.is_file(follow_symlinks=False) and (
                        _FINAL_RESULT_FILE_PATTERN.fullmatch(result_entry.name)
                        or _TEMP_RESULT_FILE_PATTERN.fullmatch(result_entry.name)
                    ):
                        managed_file_names.append(result_entry.name)
                    else:
                        invocation_unknown_count += 1
                invocation_directories.append(
                    _InvocationDirectory(
                        workspace_root=workspace_root,
                        user_name=user_name,
                        action_name=action_name,
                        tool_results_root=tool_results_root,
                        name=invocation_entry.name,
                        managed_file_names=tuple(sorted(managed_file_names)),
                        unknown_entry_count=invocation_unknown_count,
                    )
                )
                unknown_entry_count += invocation_unknown_count
            finally:
                os.close(invocation_fd)
    finally:
        os.close(tool_results_fd)
    return tuple(invocation_directories), unknown_entry_count


def _directory_entries(directory_fd: int) -> tuple[os.DirEntry[str], ...]:
    with os.scandir(directory_fd) as entries:
        return tuple(entries)


def _open_child_directory(*, parent_fd: int, name: str) -> int:
    child_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        _enforce_owner_only_directory(directory_fd=child_fd)
    except OSError:
        os.close(child_fd)
        raise
    return child_fd


def _enforce_owner_only_directory(*, directory_fd: int) -> None:
    if stat.S_IMODE(os.fstat(directory_fd).st_mode) == MANAGED_DIRECTORY_MODE:
        return
    os.fchmod(directory_fd, MANAGED_DIRECTORY_MODE)
    os.fsync(directory_fd)


def _reconcile_invocation_directory(
    *, invocation: _InvocationDirectory, referenced_paths: set[Path]
) -> _InvocationRecoveryResult:
    try:
        tool_results_fd = _open_scanned_tool_results_directory(invocation)
    except OSError as exc:
        raise ToolResultRecoveryError(
            "failed to reopen a managed Action tool-results directory"
        ) from exc
    removed_file_count = 0
    preserved_file_count = 0
    removed_directory_count = 0
    try:
        invocation_fd = _open_child_directory(
            parent_fd=tool_results_fd, name=invocation.name
        )
        try:
            for file_name in invocation.managed_file_names:
                file_path = invocation.tool_results_root / invocation.name / file_name
                file_stat = os.stat(
                    file_name, dir_fd=invocation_fd, follow_symlinks=False
                )
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ToolResultRecoveryError(
                        "managed tool-result file changed during startup recovery"
                    )
                if file_path in referenced_paths:
                    preserved_file_count += 1
                    continue
                os.unlink(file_name, dir_fd=invocation_fd)
                removed_file_count += 1
            if removed_file_count:
                os.fsync(invocation_fd)
        finally:
            os.close(invocation_fd)

        if preserved_file_count == 0 and invocation.unknown_entry_count == 0:
            os.rmdir(invocation.name, dir_fd=tool_results_fd)
            os.fsync(tool_results_fd)
            removed_directory_count = 1
    except OSError as exc:
        raise ToolResultRecoveryError(
            "failed to reconcile a managed tool-result invocation directory"
        ) from exc
    finally:
        os.close(tool_results_fd)
    return _InvocationRecoveryResult(
        removed_file_count=removed_file_count,
        removed_directory_count=removed_directory_count,
        preserved_file_count=preserved_file_count,
    )


def _open_scanned_tool_results_directory(invocation: _InvocationDirectory) -> int:
    workspace_fd = os.open(
        invocation.workspace_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        user_fd = _open_child_directory(
            parent_fd=workspace_fd,
            name=invocation.user_name,
        )
        try:
            action_fd = _open_child_directory(
                parent_fd=user_fd,
                name=invocation.action_name,
            )
            try:
                return _open_child_directory(
                    parent_fd=action_fd,
                    name=str(ACTION_TOOL_RESULTS_DIRNAME),
                )
            finally:
                os.close(action_fd)
        finally:
            os.close(user_fd)
    finally:
        os.close(workspace_fd)


__all__ = [
    "ToolResultRecoveryError",
    "ToolResultRecoveryResult",
    "reconcile_tool_results_for_startup",
]

from __future__ import annotations

import errno
import fcntl
import os
import stat
import uuid
from pathlib import Path

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    DescriptorPathMissingError,
    open_directory_descriptor,
    open_regular_file_at_descriptor,
)

from .action_session_temp_paths import resolve_action_storage_paths

ACTION_PLAN_FILENAME = "plan.md"
ACTION_PLAN_TEMP_PREFIX = f".{ACTION_PLAN_FILENAME}."
ACTION_PLAN_TEMP_SUFFIX = ".tmp"
ACTION_PLAN_MAX_BYTES = 262_144
_ACTION_PLAN_FILE_MODE = 0o600


class ActionPlanDocumentError(RuntimeError):
    """The canonical Action plan document could not be read or written."""


class ActionPlanTooLargeError(ActionPlanDocumentError):
    """The Action plan exceeds its durable document size limit."""


def is_action_plan_artifact_path(*, scratch_root: Path, path: Path) -> bool:
    relative = relative_to_directory_identity(path=path, directory=scratch_root)
    if relative is not None and relative.parts:
        if relative.parts[0].casefold() == ACTION_PLAN_FILENAME.casefold():
            return True
    if is_action_plan_temporary_name(path.name) and _same_directory(
        path.parent,
        scratch_root.parent,
    ):
        return True
    try:
        return path.samefile(scratch_root / ACTION_PLAN_FILENAME)
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ELOOP}:
            raise
        return False


def is_action_plan_temporary_name(name: str) -> bool:
    name = name.casefold()
    return name.startswith(ACTION_PLAN_TEMP_PREFIX) and name.endswith(
        ACTION_PLAN_TEMP_SUFFIX
    )


def action_plan_has_external_hardlinks(*, scratch_root: Path) -> bool:
    try:
        current = (scratch_root / ACTION_PLAN_FILENAME).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ActionPlanDocumentError(
            "failed to inspect the current Action plan"
        ) from exc
    return _has_external_hardlinks(current)


def relative_to_directory_identity(*, path: Path, directory: Path) -> Path | None:
    try:
        return path.relative_to(directory)
    except ValueError:
        parts: list[str] = []
        ancestor = path
        while ancestor.parent != ancestor:
            if (
                ancestor.name.casefold() == directory.name.casefold()
                and _same_directory(ancestor, directory)
            ):
                return Path(*reversed(parts))
            parts.append(ancestor.name)
            ancestor = ancestor.parent
        return None


def _same_directory(left: Path, right: Path) -> bool:
    return left == right or (left.is_dir() and right.is_dir() and left.samefile(right))


def read_action_plan(*, db_path: Path, user_id: str, action_id: str) -> str | None:
    workspace = resolve_action_storage_paths(
        db_path=db_path,
        user_id=user_id,
        action_id=action_id,
    ).workspace
    directory_fd = _open_workspace(workspace)
    file_fd: int | None = None
    try:
        try:
            file_fd = open_regular_file_at_descriptor(
                parent_descriptor=directory_fd,
                relative_path=ACTION_PLAN_FILENAME,
            )
        except DescriptorPathMissingError:
            return None
        try:
            if os.fstat(file_fd).st_size > ACTION_PLAN_MAX_BYTES:
                raise ActionPlanTooLargeError(
                    "stored Action plan exceeds ACTION_PLAN_MAX_BYTES"
                )
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = None
                payload = handle.read(ACTION_PLAN_MAX_BYTES + 1)
        except OSError as exc:
            raise ActionPlanDocumentError("failed to read the Action plan") from exc
        if len(payload) > ACTION_PLAN_MAX_BYTES:
            raise ActionPlanTooLargeError(
                "stored Action plan exceeds ACTION_PLAN_MAX_BYTES"
            )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ActionPlanDocumentError(
                "stored Action plan is not valid UTF-8"
            ) from exc
    except (DescriptorPathError, OSError) as exc:
        raise ActionPlanDocumentError("failed to open the Action plan") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def write_action_plan(
    *, db_path: Path, user_id: str, action_id: str, content: str
) -> None:
    try:
        payload = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionPlanDocumentError("Action plan must be valid UTF-8") from exc
    if len(payload) > ACTION_PLAN_MAX_BYTES:
        raise ActionPlanTooLargeError("Action plan exceeds ACTION_PLAN_MAX_BYTES")

    paths = resolve_action_storage_paths(
        db_path=db_path,
        user_id=user_id,
        action_id=action_id,
    )
    temp_directory_fd = _open_temp_directory(paths.action_root, fcntl.LOCK_SH)
    try:
        workspace_fd = _open_workspace(paths.workspace)
    except ActionPlanDocumentError:
        os.close(temp_directory_fd)
        raise
    try:
        _reject_linked_current_plan(workspace_fd=workspace_fd)
    except ActionPlanDocumentError:
        os.close(temp_directory_fd)
        os.close(workspace_fd)
        raise
    temporary_name = f"{ACTION_PLAN_TEMP_PREFIX}{uuid.uuid4()}{ACTION_PLAN_TEMP_SUFFIX}"
    temporary_fd: int | None = None
    renamed = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            _ACTION_PLAN_FILE_MODE,
            dir_fd=temp_directory_fd,
        )
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temporary_name,
            ACTION_PLAN_FILENAME,
            src_dir_fd=temp_directory_fd,
            dst_dir_fd=workspace_fd,
        )
        renamed = True
        os.fsync(workspace_fd)
        os.fsync(temp_directory_fd)
    except OSError as exc:
        if renamed:
            raise ActionPlanDocumentError(
                "Action plan was replaced, but directory durability could not be confirmed"
            ) from exc
        try:
            os.unlink(temporary_name, dir_fd=temp_directory_fd)
            os.fsync(temp_directory_fd)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            exc.add_note(f"Action plan temporary-file cleanup failed: {cleanup_error}")
        raise ActionPlanDocumentError(
            "failed to atomically write the Action plan"
        ) from exc
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        os.close(temp_directory_fd)
        os.close(workspace_fd)


def remove_abandoned_action_plan_write(*, action_root: Path) -> None:
    directory_fd = _open_temp_directory(action_root, fcntl.LOCK_EX)
    try:
        _remove_abandoned_writes(directory_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _remove_abandoned_writes(*, directory_fd: int) -> None:
    try:
        names = tuple(
            name
            for name in os.listdir(directory_fd)
            if is_action_plan_temporary_name(name)
        )
        for name in names:
            os.unlink(name, dir_fd=directory_fd)
    except OSError as exc:
        raise ActionPlanDocumentError(
            "failed to remove abandoned Action plan writes"
        ) from exc
    if not names:
        return
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise ActionPlanDocumentError(
            "abandoned Action plan write was removed, but directory durability "
            "could not be confirmed"
        ) from exc


def _reject_linked_current_plan(*, workspace_fd: int) -> None:
    try:
        current = os.stat(
            ACTION_PLAN_FILENAME,
            dir_fd=workspace_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ActionPlanDocumentError(
            "failed to inspect the current Action plan"
        ) from exc
    if _has_external_hardlinks(current):
        raise ActionPlanDocumentError(
            "refusing to replace an Action plan with external hard links"
        )


def _has_external_hardlinks(current: os.stat_result) -> bool:
    return stat.S_ISREG(current.st_mode) and current.st_nlink > 1


def _open_workspace(workspace: Path) -> int:
    try:
        return open_directory_descriptor(root_path=workspace)
    except (DescriptorPathError, OSError) as exc:
        raise ActionPlanDocumentError(
            "canonical Action scratch workspace is unavailable"
        ) from exc


def _open_temp_directory(action_root: Path, lock_operation: int) -> int:
    directory_fd: int | None = None
    try:
        directory_fd = open_directory_descriptor(root_path=action_root)
        fcntl.flock(directory_fd, lock_operation)
        return directory_fd
    except (DescriptorPathError, OSError) as exc:
        if directory_fd is not None:
            os.close(directory_fd)
        raise ActionPlanDocumentError(
            "host-owned Action plan temporary directory is unavailable"
        ) from exc


__all__ = [
    "ACTION_PLAN_FILENAME",
    "ACTION_PLAN_MAX_BYTES",
    "ACTION_PLAN_TEMP_PREFIX",
    "ACTION_PLAN_TEMP_SUFFIX",
    "ActionPlanDocumentError",
    "ActionPlanTooLargeError",
    "action_plan_has_external_hardlinks",
    "is_action_plan_artifact_path",
    "is_action_plan_temporary_name",
    "read_action_plan",
    "relative_to_directory_identity",
    "remove_abandoned_action_plan_write",
    "write_action_plan",
]

"""Move staged document attachments into the Action workspace on submit.

Electron main stages each file under the artifact root before the send. The
submit transaction hard-links it into ``attachments/{attachment_id}/{name}``
inside the Action's scratch workspace, where the read tools already reach, and
the staged name is unlinked only after the USER row commits. A committed Word,
PowerPoint or Excel file also starts getting the Office renderer ready, so its
pages can be drawn by the time the model asks for them.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Self

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    open_directory_descriptor,
    open_regular_file_descriptor,
)
from pantaray_agents.local_runtime.storage.transactions import register_after_commit
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
    open_durable_action_storage_child,
    resolve_action_storage_paths,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    remove_descriptor_confined_directory_child,
)
from pantaray_agents.schema.agent.action_message import (
    ACTION_ATTACHMENTS_DIRNAME,
    FileAttachmentInput,
)

from .action_message_models import ActionMessageSubmissionError
from .office_runtime import OFFICE_RUNTIME

# Electron main's attach IPC writes `{attachment_id}{ext}` here, per user.
STAGED_ATTACHMENT_DIRECTORY = "generated/attachments"
# The formats the page render tool draws by converting them with LibreOffice.
_OFFICE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})


class ActionFileAttachmentUnavailableError(ActionMessageSubmissionError):
    """A staged attachment is missing, not a plain file, or not the stated size."""


@dataclass(frozen=True, slots=True)
class _StagedFile:
    attachment: FileAttachmentInput
    identity: tuple[int, int]


class ActionFileAttachmentLinks:
    """The staged files one submit transaction links into the workspace.

    Enter it outside that transaction: when the transaction does not commit,
    the directories it created are removed on the way out.
    """

    def __init__(self) -> None:
        self._created: list[tuple[Path, str]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            return
        for parent_path, attachment_id in self._created:
            remove_descriptor_confined_directory_child(
                parent_path=parent_path, child_name=attachment_id
            )

    def link(
        self,
        *,
        connection: sqlite3.Connection,
        db_path: Path,
        artifact_root: Path,
        user_id: str,
        action_id: str,
        files: tuple[FileAttachmentInput, ...],
    ) -> None:
        """Link each staged file; unlink the staged names after commit.

        Every staged file is checked before anything is created, so a rejected
        submission leaves the workspace untouched.

        Raises:
            ActionFileAttachmentUnavailableError: a staged file cannot be used.
        """

        paths = resolve_action_storage_paths(
            db_path=db_path, user_id=user_id, action_id=action_id
        )
        attachments_path = paths.workspace / ACTION_ATTACHMENTS_DIRNAME
        staging_relative = f"{STAGED_ATTACHMENT_DIRECTORY}/{user_id}"
        staged = tuple(
            _check_staged_file(
                artifact_root=artifact_root,
                staging_relative=staging_relative,
                attachment=attachment,
            )
            for attachment in files
        )
        staging_fd = open_directory_descriptor(
            root_path=artifact_root, relative_path=staging_relative
        )
        try:
            attachments_fd = _open_workspace_attachments(
                storage_base=paths.storage_base, workspace=paths.workspace
            )
            try:
                for item in staged:
                    _link_staged_file(
                        staging_fd=staging_fd,
                        attachments_fd=attachments_fd,
                        attachments_path=attachments_path,
                        item=item,
                        created=self._created,
                    )
            finally:
                os.close(attachments_fd)
        finally:
            os.close(staging_fd)

        register_after_commit(
            connection=connection,
            callback=partial(
                _unlink_staged_files,
                artifact_root=artifact_root,
                staging_relative=staging_relative,
                names=tuple(_staged_name(file) for file in files),
            ),
        )
        if any(file.extension in _OFFICE_EXTENSIONS for file in files):
            register_after_commit(
                connection=connection,
                callback=partial(_prepare_office_renderer, paths.storage_base),
            )


def _prepare_office_renderer(storage_base: Path) -> None:
    # Discovery can wait on Spotlight, so the submit does not wait for it; the
    # install it may start already runs on a thread of its own.
    threading.Thread(
        target=OFFICE_RUNTIME.ensure_available,
        args=(storage_base,),
        name="office-runtime-ensure",
        daemon=True,
    ).start()


def _staged_name(attachment: FileAttachmentInput) -> str:
    return f"{attachment.attachment_id}{attachment.extension}"


def _check_staged_file(
    *, artifact_root: Path, staging_relative: str, attachment: FileAttachmentInput
) -> _StagedFile:
    try:
        descriptor = open_regular_file_descriptor(
            root_path=artifact_root,
            relative_path=f"{staging_relative}/{_staged_name(attachment)}",
        )
    except DescriptorPathError as exc:
        raise ActionFileAttachmentUnavailableError(
            "staged attachment is missing or not a regular file"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if file_stat.st_size != attachment.byte_size:
        raise ActionFileAttachmentUnavailableError(
            "staged attachment size does not match the message"
        )
    return _StagedFile(
        attachment=attachment, identity=(file_stat.st_dev, file_stat.st_ino)
    )


def _open_workspace_attachments(*, storage_base: Path, workspace: Path) -> int:
    """Open ``{workspace}/attachments`` without following a symlink on the way."""

    descriptor = os.open(storage_base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in (
            *workspace.relative_to(storage_base).parts,
            ACTION_ATTACHMENTS_DIRNAME,
        ):
            child = open_durable_action_storage_child(parent_fd=descriptor, name=name)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _link_staged_file(
    *,
    staging_fd: int,
    attachments_fd: int,
    attachments_path: Path,
    item: _StagedFile,
    created: list[tuple[Path, str]],
) -> None:
    attachment = item.attachment
    try:
        # An existing directory means this attachment id was already submitted.
        os.mkdir(
            attachment.attachment_id,
            mode=MANAGED_DIRECTORY_MODE,
            dir_fd=attachments_fd,
        )
    except FileExistsError as exc:
        raise ActionFileAttachmentUnavailableError(
            "attachment id was already submitted"
        ) from exc
    created.append((attachments_path, attachment.attachment_id))
    directory_fd = os.open(
        attachment.attachment_id,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=attachments_fd,
    )
    try:
        # The artifact root and the workspaces share LOCAL_DB_PATH's directory,
        # so EXDEV means a broken layout and surfaces as the OSError it is.
        os.link(
            _staged_name(attachment),
            attachment.name,
            src_dir_fd=staging_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = os.stat(attachment.name, dir_fd=directory_fd, follow_symlinks=False)
        # The staged name could have been swapped after it was checked, and a
        # symlink linked here would reach past the read scope.
        if (linked.st_dev, linked.st_ino) != item.identity:
            raise ActionFileAttachmentUnavailableError(
                "staged attachment changed while it was linked"
            )
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.fsync(attachments_fd)


def _unlink_staged_files(
    *, artifact_root: Path, staging_relative: str, names: tuple[str, ...]
) -> None:
    staging_fd = open_directory_descriptor(
        root_path=artifact_root, relative_path=staging_relative
    )
    try:
        for name in names:
            os.unlink(name, dir_fd=staging_fd)
    finally:
        os.close(staging_fd)


__all__ = [
    "ActionFileAttachmentLinks",
    "ActionFileAttachmentUnavailableError",
    "STAGED_ATTACHMENT_DIRECTORY",
]

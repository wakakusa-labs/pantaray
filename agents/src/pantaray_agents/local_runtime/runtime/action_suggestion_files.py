"""Give an Action created from a Suggestion its own copy of the Suggestion's files.

Accepting an offer and replying to a notice both create the Action in one
submit transaction. Inside it, each file the Suggestion lists is hard-linked
from the Suggestion's folder into ``suggestion/{path}`` in the Action's scratch
workspace, and the first USER message names the linked paths. From then on the
Action's copy is the one that counts: the Suggestion's folder may be removed.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Self

from pantaray_agents.local_runtime.descriptor_access import (
    DescriptorPathError,
    DescriptorPathMissingError,
    open_directory_at_descriptor,
    open_directory_descriptor,
    open_regular_file_at_descriptor,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    MANAGED_DIRECTORY_MODE,
    open_durable_action_storage_child,
    resolve_action_storage_paths,
    resolve_suggestion_artifacts_root,
)
from pantaray_agents.local_runtime.tooling.resources.action_session_temp_cleanup import (
    remove_descriptor_confined_directory_child,
)
from pantaray_agents.schema.agent.suggestion import SUGGESTION_ARTIFACTS_ADAPTER

from .action_message_models import (
    ActionMessageConflictError,
    NewActionTarget,
    SubmitActionMessageCommand,
)

logger = logging.getLogger(__name__)

# The workspace directory a Suggestion's files are linked into.
SUGGESTION_FILES_DIRNAME = "suggestion"


class ActionSuggestionFiles:
    """The Suggestion files one submit transaction links into a new Action.

    Enter it outside that transaction: when the transaction does not commit,
    the directory it created is removed on the way out.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._workspace: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None and self._workspace is not None:
            remove_descriptor_confined_directory_child(
                parent_path=self._workspace, child_name=SUGGESTION_FILES_DIRNAME
            )

    def link(
        self,
        command: SubmitActionMessageCommand,
        action_id: str,
        connection: sqlite3.Connection,
    ) -> SubmitActionMessageCommand:
        """Link the files of the Suggestion this new Action starts from.

        Returns the command whose message names the linked paths, or the
        command unchanged when it does not start from a Suggestion with files.
        A listed path that is not a regular file inside the Suggestion's folder,
        such as a symlink, is left out.

        Raises:
            ActionMessageConflictError: a file changed while it was linked.
        """

        target = command.target
        if not isinstance(target, NewActionTarget):
            return command
        suggestion_id = target.suggestion_id or target.reply_to_suggestion_id
        if suggestion_id is None:
            return command
        row = connection.execute(
            "SELECT artifacts_json FROM agent_suggestions"
            " WHERE user_id = ? AND suggestion_id = ?",
            (command.user_id, suggestion_id),
        ).fetchone()
        # A missing row is refused by the origin check of the same transaction.
        if row is None:
            return command
        artifacts = SUGGESTION_ARTIFACTS_ADAPTER.validate_json(row["artifacts_json"])
        if not artifacts:
            return command
        linked = self._link_files(
            user_id=command.user_id,
            action_id=action_id,
            suggestion_id=suggestion_id,
            artifacts=artifacts,
        )
        if len(linked) < len(artifacts):
            logger.warning(
                "Suggestion %s lists %d files that are not regular files in its "
                "folder; they were not copied",
                suggestion_id,
                len(artifacts) - len(linked),
            )
        if not linked:
            return command
        message = command.message.model_copy(update={"suggestion_files": linked})
        return command.model_copy(update={"message": message})

    def _link_files(
        self,
        *,
        user_id: str,
        action_id: str,
        suggestion_id: str,
        artifacts: list[str],
    ) -> tuple[str, ...]:
        try:
            source_fd = open_directory_descriptor(
                root_path=resolve_suggestion_artifacts_root(
                    db_path=self._db_path, user_id=user_id, suggestion_id=suggestion_id
                )
            )
        except DescriptorPathMissingError:
            return ()
        try:
            paths = resolve_action_storage_paths(
                db_path=self._db_path, user_id=user_id, action_id=action_id
            )
            self._workspace = paths.workspace
            target_fd = _create_suggestion_directory(
                storage_base=paths.storage_base, workspace=paths.workspace
            )
            try:
                return tuple(
                    f"{SUGGESTION_FILES_DIRNAME}/{path}"
                    for path in artifacts
                    if _link_file(source_fd=source_fd, target_fd=target_fd, path=path)
                )
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)


def _create_suggestion_directory(*, storage_base: Path, workspace: Path) -> int:
    """Create ``{workspace}/suggestion`` without following a symlink on the way."""

    descriptor = os.open(storage_base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in workspace.relative_to(storage_base).parts:
            child = open_durable_action_storage_child(parent_fd=descriptor, name=name)
            os.close(descriptor)
            descriptor = child
        # The Action is new, so an existing directory is not one to reuse.
        os.mkdir(
            SUGGESTION_FILES_DIRNAME, mode=MANAGED_DIRECTORY_MODE, dir_fd=descriptor
        )
        os.fsync(descriptor)
        return os.open(
            SUGGESTION_FILES_DIRNAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)


def _link_file(*, source_fd: int, target_fd: int, path: str) -> bool:
    *parents, name = path.split("/")
    try:
        source_parent = open_directory_at_descriptor(
            parent_descriptor=source_fd, relative_path="/".join(parents) or "."
        )
    except DescriptorPathError:
        return False
    try:
        try:
            file_fd = open_regular_file_at_descriptor(
                parent_descriptor=source_parent, relative_path=name
            )
        except DescriptorPathError:
            return False
        try:
            source = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        target_parent = os.dup(target_fd)
        try:
            for directory in parents:
                child = open_durable_action_storage_child(
                    parent_fd=target_parent, name=directory
                )
                os.close(target_parent)
                target_parent = child
            # The storage base holds both folders, so a link never crosses devices.
            os.link(
                name,
                name,
                src_dir_fd=source_parent,
                dst_dir_fd=target_parent,
                follow_symlinks=False,
            )
            linked = os.stat(name, dir_fd=target_parent, follow_symlinks=False)
            # The name could have been swapped after it was checked, and a
            # symlink linked here would reach past the workspace.
            if (linked.st_dev, linked.st_ino) != (source.st_dev, source.st_ino):
                raise ActionMessageConflictError(
                    "Suggestion file changed while it was linked"
                )
            os.fsync(target_parent)
        finally:
            os.close(target_parent)
    finally:
        os.close(source_parent)
    return True


__all__ = ["SUGGESTION_FILES_DIRNAME", "ActionSuggestionFiles"]

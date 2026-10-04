"""Folders outside the workspace that the user allowed for one conversation.

"Allow for this conversation" on an outside-workspace approval makes each
approved folder a writable root of that Action's workspace manifest. Later calls
of the Action resolve inside it like a registered folder and follow the normal
approval mode; other Actions never see it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pantaray_agents.local_runtime.runtime.runtime_env import (
    read_local_runtime_artifact_root,
)
from pantaray_agents.schema.agent.base import JSONValue

from .action_session_temp_paths import resolve_local_runtime_storage_base
from .brokering.command_approval_summaries import outside_workspace_folder_paths
from .repository.manifests import insert_approved_folder_root_in_connection
from .workspace_manifest_roots import ManifestRoot
from .workspace_root_authority import (
    WorkspaceRootAuthorityError,
    validate_workspace_root,
)

APPROVED_FOLDER_SOURCE_TYPE = "approved_folder"


class OutsideWorkspaceGrantError(ValueError):
    """The approval cannot open its folder for the rest of the conversation."""


def app_owned_roots(db_path: Path) -> tuple[Path, ...]:
    """Pantaray's private app storage: the app data folder and the artifact root."""

    return (
        resolve_local_runtime_storage_base(db_path=db_path),
        read_local_runtime_artifact_root().resolve(),
    )


def path_is_within(*, path: Path, root: Path) -> bool:
    if path.is_relative_to(root):
        return True
    # APFS case/Unicode aliases need directory identity, not lexical comparison.
    for candidate in (path, *path.parents):
        try:
            if candidate.samefile(root):
                return True
        except OSError:
            continue
    return False


def folder_can_be_granted(*, folder: Path, db_path: Path) -> bool:
    """Whether commands may write anywhere under this folder.

    A folder that is not an existing canonical directory, or that lies inside
    or encloses app-owned storage (/, the home folder, ~/Library...), is refused.
    """

    try:
        validate_workspace_root(real_path=folder, canonical_real_path=folder)
    except WorkspaceRootAuthorityError:
        return False
    return not any(
        path_is_within(path=folder, root=root) or path_is_within(path=root, root=folder)
        for root in app_owned_roots(db_path)
    )


def approved_summary_covers_request(
    *,
    approved: dict[str, JSONValue],
    requested: dict[str, JSONValue],
    manifest_roots: tuple[ManifestRoot, ...],
) -> bool:
    """Whether a stored approval summary still describes this tool request."""

    if approved == requested:
        return True
    # After "Allow for this conversation" the approved call resolves inside the
    # newly granted roots, so its summary no longer names those folders. A later
    # registration of the same folder replaces its root with a folder root at the
    # next reconcile, which still covers the approved call.
    approved_folders = set(outside_workspace_folder_paths(approved))
    if not approved_folders:
        return False
    requested_folders = set(outside_workspace_folder_paths(requested))
    if _without_outside_folders(approved) != _without_outside_folders(requested):
        return False
    if not requested_folders <= approved_folders:
        return False
    granted_paths = {
        str(root.canonical_real_path)
        for root in manifest_roots
        if root.source_type in {APPROVED_FOLDER_SOURCE_TYPE, "folder"}
    }
    return approved_folders - requested_folders <= granted_paths


def _without_outside_folders(summary: dict[str, JSONValue]) -> dict[str, JSONValue]:
    return {key: value for key, value in summary.items() if key != "outside_workspace"}


def grant_outside_workspace_folder_in_connection(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    action_id: str,
    approval_session_id: str,
    db_path: Path,
    granted_at: str,
) -> None:
    """Add each of the approval's folders as a writable root of the Action's manifest.

    The folders come from the stored approval summary, never from the client.
    """

    row = connection.execute(
        """
        SELECT manifest.manifest_id, approval.command_summary_json
        FROM approval_sessions AS approval
        JOIN workspace_manifests AS manifest
          ON manifest.manifest_id = approval.manifest_id
         AND manifest.user_id = approval.user_id
         AND manifest.action_id = approval.action_id
         AND manifest.status = 'ready'
        WHERE approval.approval_session_id = ?
          AND approval.user_id = ?
          AND approval.action_id = ?
        """,
        (approval_session_id, user_id, action_id),
    ).fetchone()
    if row is None:
        raise OutsideWorkspaceGrantError("approval has no ready workspace manifest")
    folders = tuple(
        Path(path) for path in outside_workspace_folder_paths(json.loads(str(row[1])))
    )
    if not folders:
        raise OutsideWorkspaceGrantError(
            "approval does not open a folder outside the workspace"
        )
    if not all(
        folder_can_be_granted(folder=folder, db_path=db_path) for folder in folders
    ):
        raise OutsideWorkspaceGrantError(
            "folder cannot be allowed for the whole conversation"
        )
    for ordinal, folder in enumerate(folders):
        insert_approved_folder_root_in_connection(
            connection,
            manifest_id=str(row[0]),
            approval_session_id=approval_session_id,
            ordinal=ordinal,
            folder=folder,
            created_at=granted_at,
        )


__all__ = [
    "APPROVED_FOLDER_SOURCE_TYPE",
    "OutsideWorkspaceGrantError",
    "app_owned_roots",
    "approved_summary_covers_request",
    "folder_can_be_granted",
    "grant_outside_workspace_folder_in_connection",
    "path_is_within",
]

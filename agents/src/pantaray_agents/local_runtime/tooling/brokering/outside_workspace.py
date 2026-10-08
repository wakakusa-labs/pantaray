"""Local folders outside every manifest root, usable only after approval.

An approved folder opens for the approved tool call alone: as a one-off patch
root for apply_patch, or as an extra process read/write root for a bash or
run_python call whose cwd it is. Allowed for the conversation, it becomes a
manifest root instead (see outside_workspace_grant). App-owned storage and
symlink escapes out of a workspace root are never approvable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.tools.files.manifest_paths import (
    ManifestRoot,
    ResolvedManifestPath,
)

from ..outside_workspace_grant import (
    app_owned_roots,
    folder_can_be_granted,
    path_is_within,
)
from .broker_common import BrokerContext


@dataclass(frozen=True, slots=True)
class OutsideWorkspacePatchTarget:
    path: Path
    folder: Path


@dataclass(frozen=True, slots=True)
class OutsideWorkspaceCwd:
    path: Path


def resolve_outside_workspace_patch_target(
    *,
    context: BrokerContext,
    candidate: Path,
) -> OutsideWorkspacePatchTarget | None:
    """Return the approvable target for a path that belongs to no manifest root.

    None means the path stays hard-denied.
    """

    if _is_symlink_escape(context=context, candidate=candidate):
        return None
    target = candidate.resolve(strict=False)
    if not _is_approvable_folder(context=context, folder=target.parent):
        return None
    return OutsideWorkspacePatchTarget(path=target, folder=target.parent)


def resolve_outside_workspace_cwd(
    *,
    context: BrokerContext,
    candidate: Path,
) -> OutsideWorkspaceCwd | None:
    """Return the approvable command cwd that belongs to no manifest root.

    None means the cwd stays hard-denied.
    """

    if _is_symlink_escape(context=context, candidate=candidate):
        return None
    folder = candidate.resolve(strict=False)
    # A command may write anywhere under its cwd, so the cwd must pass the same
    # rule as a folder allowed for the whole conversation. apply_patch writes
    # only the file shown, so it keeps accepting parents that enclose app storage.
    if not folder_can_be_granted(folder=folder, db_path=context.db_path):
        return None
    return OutsideWorkspaceCwd(path=folder)


def _is_symlink_escape(*, context: BrokerContext, candidate: Path) -> bool:
    # Written under a workspace root but resolving outside it.
    lexical = Path(os.path.normpath(candidate))
    return any(
        path_is_within(path=lexical, root=root.canonical_real_path)
        for root in context.manifest_roots
    )


def _is_approvable_folder(*, context: BrokerContext, folder: Path) -> bool:
    if not folder.is_dir():
        return False
    return not any(
        path_is_within(path=folder, root=root)
        for root in app_owned_roots(context.db_path)
    )


def outside_workspace_resolved_path(
    *,
    context: BrokerContext,
    target: OutsideWorkspacePatchTarget,
) -> ResolvedManifestPath:
    root = ManifestRoot(
        root_id=f"outside_workspace:{target.folder}",
        manifest_id=context.manifest_id,
        source_type="local_filesystem",
        display_name=target.folder.name,
        canonical_real_path=target.folder,
        real_path=target.folder,
        can_read=True,
        can_apply_patch=True,
        can_process_read=False,
        can_process_write=False,
    )
    return ResolvedManifestPath(
        root=root,
        path=target.path,
        root_relative_path=target.path.name,
    )


__all__ = [
    "OutsideWorkspaceCwd",
    "OutsideWorkspacePatchTarget",
    "outside_workspace_resolved_path",
    "resolve_outside_workspace_cwd",
    "resolve_outside_workspace_patch_target",
]

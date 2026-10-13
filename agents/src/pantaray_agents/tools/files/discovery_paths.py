from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import rignore

from .manifest_paths import ResolvedManifestPath
from .read_contract import DiscoveryTruncationReason
from .ripgrep import ALWAYS_EXCLUDED_NAME
from .workspace_descriptor_access import WorkspaceScanSkips, scan_workspace_entries


@dataclass(frozen=True, slots=True)
class DiscoveryPath:
    path: Path
    kind: Literal["file", "directory"]


@dataclass(frozen=True, slots=True)
class BoundedDiscoveryPaths:
    selected: list[DiscoveryPath]
    truncation_reason: DiscoveryTruncationReason | None
    skips: WorkspaceScanSkips


def list_discovery_paths(
    *,
    base: ResolvedManifestPath,
    max_depth: int | None,
    limit: int,
    include_path: Callable[[Path], bool],
    exclude_subtree: Callable[[Path], bool],
) -> BoundedDiscoveryPaths:
    ignored = _ignored_by_discovery_rules()
    result = scan_workspace_entries(
        root_path=base.root.canonical_real_path,
        base_path=base.root_relative_path,
        max_depth=max_depth,
        limit=limit,
        include_path=include_path,
        exclude_subtree=lambda path: exclude_subtree(path) or ignored(path),
    )
    return BoundedDiscoveryPaths(
        selected=[
            DiscoveryPath(
                path=base.root.canonical_real_path.joinpath(
                    *entry.root_relative_path.split("/")
                ),
                kind=entry.kind,
            )
            for entry in result.entries
        ],
        truncation_reason=result.truncation_reason,
        skips=result.skips,
    )


def entry_for_discovery_path(path: DiscoveryPath) -> dict[str, object]:
    return {
        "path": str(path.path),
        "kind": path.kind,
        "name": path.path.name,
    }


def _ignored_by_discovery_rules() -> Callable[[Path], bool]:
    """Whether glob and grep leave a path out, by the rules in ``ripgrep``.

    ripgrep prints no directories, so the same ignore library answers here.
    Each directory's entries are asked for once, as the scan reaches it.
    """

    shown: dict[Path, frozenset[str]] = {}

    def ignored(path: Path) -> bool:
        directory = path.parent
        names = shown.get(directory)
        if names is None:
            names = frozenset(
                entry.name
                for entry in rignore.walk(
                    directory,
                    ignore_hidden=False,
                    read_ignore_files=True,
                    read_parents_ignores=True,
                    read_git_ignore=True,
                    read_global_git_ignore=False,
                    read_git_exclude=True,
                    require_git=True,
                    overrides=[f"!{ALWAYS_EXCLUDED_NAME}"],
                    max_depth=1,
                    follow_links=False,
                )
                if entry != directory
            )
            shown[directory] = names
        return path.name not in names

    return ignored

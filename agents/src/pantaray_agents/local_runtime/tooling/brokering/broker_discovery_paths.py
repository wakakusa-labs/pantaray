from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .manifest_paths import ResolvedManifestPath
from .workspace_descriptor_access import WorkspaceScanSkips, scan_workspace_entries

type DiscoveryTruncationReason = Literal[
    "limit",
    "timeout",
    "output_bytes",
    "line_length",
]


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
    result = scan_workspace_entries(
        root_path=base.root.canonical_real_path,
        base_path=base.root_relative_path,
        max_depth=max_depth,
        limit=limit,
        include_path=include_path,
        exclude_subtree=exclude_subtree,
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

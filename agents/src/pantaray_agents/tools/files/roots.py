from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemorySource,
)


@dataclass(frozen=True, slots=True)
class WorkspaceReadRoot:
    root_id: str
    display_name: str
    canonical_path: Path
    # Pantaray's own storage, hidden when a registered folder contains it.
    private_app_storage: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class MemoryReadRoot:
    root_id: str
    display_name: str
    revision_id: str
    node_id: str
    entry_path: str
    documents: tuple[MemoryDocument, ...]


type ReadOnlyRoot = WorkspaceReadRoot | MemoryReadRoot


def memory_revision_by_source(
    roots: tuple[ReadOnlyRoot, ...],
) -> dict[MemorySource, str | None]:
    revisions: dict[MemorySource, str | None] = {
        "fact": None,
        "long_term_insight": None,
        "agent_experience": None,
    }
    for root in roots:
        if not isinstance(root, MemoryReadRoot):
            continue
        source: MemorySource
        if root.root_id == "facts":
            source = "fact"
        elif root.root_id == "insights":
            source = "long_term_insight"
        elif root.root_id == "agent_experience":
            source = "agent_experience"
        else:
            continue
        revisions[source] = root.revision_id
    return revisions


__all__ = [
    "MemoryReadRoot",
    "ReadOnlyRoot",
    "WorkspaceReadRoot",
    "memory_revision_by_source",
]

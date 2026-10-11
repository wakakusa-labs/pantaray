from __future__ import annotations

from dataclasses import dataclass

from pantaray_agents.schema.read_access import ReadAccessScope

from ...storage.migrations import MigrationError


class WorkspaceProjectNameTakenError(MigrationError):
    """Another project of this user already has the name."""


@dataclass(frozen=True)
class WorkspaceOrganization:
    organization_id: str
    display_name: str


@dataclass(frozen=True)
class WorkspaceProject:
    project_id: str
    display_name: str
    sort_order: int
    organization_ids: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceFolder:
    folder_id: str
    display_name: str
    real_path: str
    canonical_real_path: str
    organization_ids: tuple[str, ...]
    project_ids: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceSettings:
    read_access_scope: ReadAccessScope
    organizations: tuple[WorkspaceOrganization, ...]
    projects: tuple[WorkspaceProject, ...]
    folders: tuple[WorkspaceFolder, ...]


__all__ = [
    "WorkspaceFolder",
    "WorkspaceOrganization",
    "WorkspaceProject",
    "WorkspaceProjectNameTakenError",
    "WorkspaceSettings",
]

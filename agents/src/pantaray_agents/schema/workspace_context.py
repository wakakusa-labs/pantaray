from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict, cast

from pantaray_agents.schema.read_access import (
    READ_ACCESS_SCOPE_FULL_ACCESS,
    READ_ACCESS_SCOPE_WORKSPACE,
    ReadAccessScope,
)


@dataclass(frozen=True, slots=True)
class WorkspaceContextOrganization:
    display_name: str


@dataclass(frozen=True, slots=True)
class WorkspaceContextProject:
    display_name: str
    organization_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceContextFolder:
    display_name: str
    path: str
    organization_names: tuple[str, ...]
    project_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceContextCatalog:
    organizations: tuple[WorkspaceContextOrganization, ...]
    projects: tuple[WorkspaceContextProject, ...]
    folders: tuple[WorkspaceContextFolder, ...]
    read_access_scope: ReadAccessScope = READ_ACCESS_SCOPE_WORKSPACE


class WorkspaceContextOrganizationSnapshot(TypedDict):
    display_name: str


class WorkspaceContextProjectSnapshot(TypedDict):
    display_name: str
    organization_names: list[str]


class WorkspaceContextFolderSnapshot(TypedDict):
    display_name: str
    path: str
    organization_names: list[str]
    project_names: list[str]


class WorkspaceContextSnapshot(TypedDict):
    organizations: list[WorkspaceContextOrganizationSnapshot]
    projects: list[WorkspaceContextProjectSnapshot]
    folders: list[WorkspaceContextFolderSnapshot]


def render_workspace_context_prompt(catalog: WorkspaceContextCatalog) -> str:
    lines: list[str] = [
        _format_read_access(catalog.read_access_scope),
        "Edit/command access: workspace roots only",
    ]
    if catalog.organizations:
        lines.append("")
        lines.append("Organizations:")
        lines.extend(
            f"- {organization.display_name}" for organization in catalog.organizations
        )
    if catalog.projects:
        if lines:
            lines.append("")
        lines.append("Projects:")
        lines.extend(_format_project(project) for project in catalog.projects)
    if catalog.folders:
        if lines:
            lines.append("")
        lines.append("Folders:")
        lines.extend(_format_folder(folder) for folder in catalog.folders)
    return "\n".join(lines)


def render_workspace_structure_prompt(catalog: WorkspaceContextCatalog) -> str:
    lines: list[str] = []
    if catalog.organizations:
        lines.append("Organizations:")
        lines.extend(
            f"- {organization.display_name}" for organization in catalog.organizations
        )
    if catalog.projects:
        if lines:
            lines.append("")
        lines.append("Projects:")
        lines.extend(_format_project(project) for project in catalog.projects)
    if catalog.folders:
        if lines:
            lines.append("")
        lines.append("Registered workspace roots:")
        lines.extend(_format_folder(folder) for folder in catalog.folders)
    return "\n".join(lines) if lines else "(No registered workspace structure)"


def workspace_context_catalog_to_snapshot(
    catalog: WorkspaceContextCatalog,
) -> WorkspaceContextSnapshot:
    return {
        "organizations": [
            {"display_name": organization.display_name}
            for organization in catalog.organizations
        ],
        "projects": [
            {
                "display_name": project.display_name,
                "organization_names": list(project.organization_names),
            }
            for project in catalog.projects
        ],
        "folders": [
            {
                "display_name": folder.display_name,
                "path": folder.path,
                "organization_names": list(folder.organization_names),
                "project_names": list(folder.project_names),
            }
            for folder in catalog.folders
        ],
    }


def workspace_context_catalog_from_snapshot(
    snapshot: object,
    *,
    read_access_scope: ReadAccessScope,
) -> WorkspaceContextCatalog:
    raw = _require_mapping(snapshot, "workspace_context_snapshot")
    return WorkspaceContextCatalog(
        organizations=tuple(
            WorkspaceContextOrganization(
                display_name=_read_string(item, "organizations[].display_name")
            )
            for item in _read_sequence(raw, "organizations")
        ),
        projects=tuple(
            WorkspaceContextProject(
                display_name=_read_string(item, "projects[].display_name"),
                organization_names=_read_string_tuple(
                    item,
                    "organization_names",
                    "projects[].organization_names",
                ),
            )
            for item in _read_sequence(raw, "projects")
        ),
        folders=tuple(
            WorkspaceContextFolder(
                display_name=_read_string(item, "folders[].display_name"),
                path=_read_string(item, "folders[].path", key="path"),
                organization_names=_read_string_tuple(
                    item,
                    "organization_names",
                    "folders[].organization_names",
                ),
                project_names=_read_string_tuple(
                    item,
                    "project_names",
                    "folders[].project_names",
                ),
            )
            for item in _read_sequence(raw, "folders")
        ),
        read_access_scope=read_access_scope,
    )


def _format_read_access(read_access_scope: ReadAccessScope) -> str:
    if read_access_scope == READ_ACCESS_SCOPE_FULL_ACCESS:
        return "Read/search access: full local filesystem"
    return "Read/search access: workspace roots only"


def _format_project(project: WorkspaceContextProject) -> str:
    if not project.organization_names:
        return f"- {project.display_name}"
    return (
        f"- {project.display_name} "
        f"(organizations: {', '.join(project.organization_names)})"
    )


def _format_folder(folder: WorkspaceContextFolder) -> str:
    metadata = _join_metadata(
        (
            ("projects", folder.project_names),
            ("organizations", folder.organization_names),
        )
    )
    suffix = f" ({metadata})" if metadata else ""
    return f"- {folder.display_name}: {folder.path}{suffix}"


def _join_metadata(
    sections: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    parts = [f"{label}: {', '.join(values)}" for label, values in sections if values]
    return "; ".join(parts)


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return cast(Mapping[str, object], value)


def _read_sequence(
    raw: Mapping[str, object],
    key: str,
) -> tuple[Mapping[str, object], ...]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"workspace_context_snapshot.{key} must be an array")
    return tuple(
        _require_mapping(item, f"workspace_context_snapshot.{key}[]") for item in value
    )


def _read_string(
    raw: Mapping[str, object],
    field_name: str,
    *,
    key: str = "display_name",
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"workspace_context_snapshot.{field_name} must be a string")
    return value


def _read_string_tuple(
    raw: Mapping[str, object],
    key: str,
    field_name: str,
) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            f"workspace_context_snapshot.{field_name} must be a string array"
        )
    return tuple(value)


__all__ = [
    "WorkspaceContextCatalog",
    "WorkspaceContextFolder",
    "WorkspaceContextOrganization",
    "WorkspaceContextProject",
    "WorkspaceContextSnapshot",
    "render_workspace_context_prompt",
    "render_workspace_structure_prompt",
    "workspace_context_catalog_from_snapshot",
    "workspace_context_catalog_to_snapshot",
]

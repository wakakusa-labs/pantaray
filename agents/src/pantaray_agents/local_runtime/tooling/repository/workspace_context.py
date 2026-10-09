from __future__ import annotations

from pathlib import Path

from pantaray_agents.schema.workspace_context import (
    WorkspaceContextCatalog,
    WorkspaceContextFolder,
    WorkspaceContextOrganization,
    WorkspaceContextProject,
    render_workspace_context_prompt,
    render_workspace_structure_prompt,
)

from .workspace_settings import list_workspace_settings
from .workspace_settings_models import WorkspaceSettings


def load_workspace_context_catalog(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> WorkspaceContextCatalog:
    settings = list_workspace_settings(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
    )
    return build_workspace_context_catalog(settings)


def load_workspace_context_prompt(
    *,
    db_path: Path,
    busy_timeout_ms: int,
    user_id: str,
) -> str:
    settings = list_workspace_settings(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )
    return build_workspace_context_prompt(settings)


def build_workspace_context_prompt(settings: WorkspaceSettings) -> str:
    return render_workspace_context_prompt(build_workspace_context_catalog(settings))


def load_workspace_structure_prompt(
    *, db_path: Path, busy_timeout_ms: int, user_id: str
) -> str:
    catalog = load_workspace_context_catalog(
        db_path=db_path,
        busy_timeout_ms=busy_timeout_ms,
        user_id=user_id,
    )
    return render_workspace_structure_prompt(catalog)


def build_workspace_context_catalog(
    settings: WorkspaceSettings,
) -> WorkspaceContextCatalog:
    organization_names_by_id = {
        organization.organization_id: organization.display_name
        for organization in settings.organizations
    }
    project_names_by_id = {
        project.project_id: project.display_name for project in settings.projects
    }
    project_organization_ids_by_id = {
        project.project_id: project.organization_ids for project in settings.projects
    }
    return WorkspaceContextCatalog(
        organizations=tuple(
            WorkspaceContextOrganization(display_name=organization.display_name)
            for organization in settings.organizations
        ),
        projects=tuple(
            WorkspaceContextProject(
                display_name=project.display_name,
                organization_names=_resolve_names(
                    project.organization_ids,
                    organization_names_by_id,
                ),
            )
            for project in settings.projects
        ),
        folders=tuple(
            WorkspaceContextFolder(
                display_name=folder.display_name,
                path=folder.real_path,
                organization_names=_resolve_names(
                    _folder_organization_ids(
                        folder_project_ids=folder.project_ids,
                        folder_organization_ids=folder.organization_ids,
                        project_organization_ids_by_id=project_organization_ids_by_id,
                    ),
                    organization_names_by_id,
                ),
                project_names=_resolve_names_in_catalog_order(
                    folder.project_ids,
                    project_names_by_id,
                ),
            )
            for folder in settings.folders
        ),
        read_access_scope=settings.read_access_scope,
    )


def _resolve_names(
    item_ids: tuple[str, ...],
    names_by_id: dict[str, str],
) -> tuple[str, ...]:
    return tuple(
        sorted(names_by_id[item_id] for item_id in item_ids if item_id in names_by_id)
    )


def _resolve_names_in_catalog_order(
    item_ids: tuple[str, ...],
    names_by_id: dict[str, str],
) -> tuple[str, ...]:
    selected_ids = frozenset(item_ids)
    return tuple(
        display_name
        for item_id, display_name in names_by_id.items()
        if item_id in selected_ids
    )


def _folder_organization_ids(
    *,
    folder_project_ids: tuple[str, ...],
    folder_organization_ids: tuple[str, ...],
    project_organization_ids_by_id: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    linked_project_organization_ids = {
        organization_id
        for project_id in folder_project_ids
        for organization_id in project_organization_ids_by_id.get(project_id, ())
    }
    if any(
        project_id in project_organization_ids_by_id
        for project_id in folder_project_ids
    ):
        return tuple(sorted(linked_project_organization_ids))
    return folder_organization_ids


__all__ = [
    "build_workspace_context_catalog",
    "build_workspace_context_prompt",
    "load_workspace_context_catalog",
    "load_workspace_context_prompt",
    "load_workspace_structure_prompt",
]

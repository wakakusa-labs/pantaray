from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.repository import (
    ReadAccessScope,
    WorkspaceFolder,
    WorkspaceOrganization,
    WorkspaceProject,
    WorkspaceSettings,
    create_workspace_folder,
    create_workspace_organization,
    create_workspace_project,
    delete_workspace_folder,
    delete_workspace_organization,
    delete_workspace_project,
    list_workspace_settings,
    load_read_access_scope,
    replace_workspace_folder_links,
    replace_workspace_project_links,
    replace_workspace_project_order,
    update_read_access_scope,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    load_command_network_enabled,
    update_command_network_enabled,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Workspace Settings"])


class WorkspaceOrganizationResponse(BaseModel):
    organization_id: str
    display_name: str


class WorkspaceProjectResponse(BaseModel):
    project_id: str
    display_name: str
    sort_order: int
    organization_ids: tuple[str, ...]


class WorkspaceFolderResponse(BaseModel):
    folder_id: str
    display_name: str
    real_path: str
    canonical_real_path: str
    organization_ids: tuple[str, ...]
    project_ids: tuple[str, ...]


class WorkspaceSettingsResponse(BaseModel):
    read_access_scope: ReadAccessScope
    organizations: tuple[WorkspaceOrganizationResponse, ...]
    projects: tuple[WorkspaceProjectResponse, ...]
    folders: tuple[WorkspaceFolderResponse, ...]


class WorkspaceOrganizationCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)


WorkspaceProjectOrganizationIds = Annotated[tuple[str, ...], Field(max_length=1)]


class WorkspaceProjectCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)
    organization_ids: WorkspaceProjectOrganizationIds = ()


class WorkspaceFolderCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)
    real_path: str = Field(min_length=1)
    organization_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()


class WorkspaceProjectLinksUpdateRequest(BaseModel):
    organization_ids: WorkspaceProjectOrganizationIds = ()


class WorkspaceFolderLinksUpdateRequest(BaseModel):
    organization_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()


WorkspaceProjectId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class WorkspaceProjectOrderUpdateRequest(BaseModel):
    project_ids: tuple[WorkspaceProjectId, ...] = Field(max_length=1_000)

    @field_validator("project_ids")
    @classmethod
    def reject_duplicate_project_ids(
        cls,
        project_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(project_ids) != len(set(project_ids)):
            raise ValueError("project_ids must not contain duplicates")
        return project_ids


class WorkspaceProjectOrderResponse(BaseModel):
    project_ids: tuple[str, ...]


class ReadAccessScopeUpdateRequest(BaseModel):
    read_access_scope: ReadAccessScope


class CommandNetworkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command_network_enabled: bool


def _assert_user_allowed(*, user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id mismatch",
        )


def _settings_response(settings: WorkspaceSettings) -> WorkspaceSettingsResponse:
    return WorkspaceSettingsResponse(
        read_access_scope=settings.read_access_scope,
        organizations=tuple(
            _organization_response(item) for item in settings.organizations
        ),
        projects=tuple(_project_response(item) for item in settings.projects),
        folders=tuple(_folder_response(item) for item in settings.folders),
    )


def _organization_response(
    organization: WorkspaceOrganization,
) -> WorkspaceOrganizationResponse:
    return WorkspaceOrganizationResponse(
        organization_id=organization.organization_id,
        display_name=organization.display_name,
    )


def _project_response(project: WorkspaceProject) -> WorkspaceProjectResponse:
    return WorkspaceProjectResponse(
        project_id=project.project_id,
        display_name=project.display_name,
        sort_order=project.sort_order,
        organization_ids=project.organization_ids,
    )


def _folder_response(folder: WorkspaceFolder) -> WorkspaceFolderResponse:
    return WorkspaceFolderResponse(
        folder_id=folder.folder_id,
        display_name=folder.display_name,
        real_path=folder.real_path,
        canonical_real_path=folder.canonical_real_path,
        organization_ids=folder.organization_ids,
        project_ids=folder.project_ids,
    )


@router.get(
    "/{user_id}/workspace-settings",
    response_model=WorkspaceSettingsResponse,
)
async def get_workspace_settings(
    user_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceSettingsResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return _settings_response(
            list_workspace_settings(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
            )
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load workspace settings: {exc}",
        ) from exc


@router.get(
    "/{user_id}/workspace-settings/read-access-scope",
)
async def get_read_access_scope(
    user_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> dict[str, ReadAccessScope]:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        read_access_scope = load_read_access_scope(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
        )
        return {"read_access_scope": read_access_scope}
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load read access scope: {exc}",
        ) from exc


@router.put(
    "/{user_id}/workspace-settings/read-access-scope",
)
async def put_read_access_scope(
    user_id: str,
    body: ReadAccessScopeUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> dict[str, ReadAccessScope]:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        read_access_scope = update_read_access_scope(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            read_access_scope=body.read_access_scope,
            now=now_utc_iso(),
        )
        return {"read_access_scope": read_access_scope}
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update read access scope: {exc}",
        ) from exc


@router.get("/{user_id}/workspace-settings/command-network")
async def get_command_network_settings(
    user_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> CommandNetworkSettings:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        enabled = load_command_network_enabled(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        )
    except (MigrationError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load command network settings",
        ) from exc
    return CommandNetworkSettings(command_network_enabled=enabled)


@router.put("/{user_id}/workspace-settings/command-network")
async def put_command_network_settings(
    user_id: str,
    body: CommandNetworkSettings,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> CommandNetworkSettings:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        enabled = update_command_network_enabled(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            command_network_enabled=body.command_network_enabled,
            now=now_utc_iso(),
        )
    except (MigrationError, sqlite3.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save command network settings",
        ) from exc
    return CommandNetworkSettings(command_network_enabled=enabled)


@router.post(
    "/{user_id}/workspace-settings/organizations",
    response_model=WorkspaceOrganizationResponse,
)
async def post_workspace_organization(
    user_id: str,
    body: WorkspaceOrganizationCreateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceOrganizationResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return _organization_response(
            create_workspace_organization(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                display_name=body.display_name,
                now=now_utc_iso(),
            )
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create workspace organization: {exc}",
        ) from exc


@router.post(
    "/{user_id}/workspace-settings/projects",
    response_model=WorkspaceProjectResponse,
)
async def post_workspace_project(
    user_id: str,
    body: WorkspaceProjectCreateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceProjectResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return _project_response(
            create_workspace_project(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                display_name=body.display_name,
                organization_ids=body.organization_ids,
                now=now_utc_iso(),
            )
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create workspace project: {exc}",
        ) from exc


@router.put(
    "/{user_id}/workspace-settings/projects/order",
    response_model=WorkspaceProjectOrderResponse,
)
async def put_workspace_project_order(
    user_id: str,
    body: WorkspaceProjectOrderUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceProjectOrderResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return WorkspaceProjectOrderResponse(
            project_ids=replace_workspace_project_order(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                project_ids=body.project_ids,
                now=now_utc_iso(),
            )
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update workspace project order: {exc}",
        ) from exc


@router.delete(
    "/{user_id}/workspace-settings/organizations/{organization_id}",
)
async def delete_workspace_organization_route(
    user_id: str,
    organization_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> dict[str, bool]:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        delete_workspace_organization(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            organization_id=organization_id,
        )
        return {"ok": True}
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Failed to delete workspace organization: {exc}",
        ) from exc


@router.delete(
    "/{user_id}/workspace-settings/projects/{project_id}",
)
async def delete_workspace_project_route(
    user_id: str,
    project_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> dict[str, bool]:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        delete_workspace_project(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            project_id=project_id,
        )
        return {"ok": True}
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Failed to delete workspace project: {exc}",
        ) from exc


@router.delete(
    "/{user_id}/workspace-settings/folders/{folder_id}",
)
async def delete_workspace_folder_route(
    user_id: str,
    folder_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> dict[str, bool]:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        delete_workspace_folder(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            folder_id=folder_id,
        )
        return {"ok": True}
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Failed to delete workspace folder: {exc}",
        ) from exc


@router.post(
    "/{user_id}/workspace-settings/folders",
    response_model=WorkspaceFolderResponse,
)
async def post_workspace_folder(
    user_id: str,
    body: WorkspaceFolderCreateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceFolderResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return _folder_response(
            create_workspace_folder(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                real_path=Path(body.real_path),
                display_name=body.display_name,
                organization_ids=body.organization_ids,
                project_ids=body.project_ids,
                now=now_utc_iso(),
            )
        )
    except (MigrationError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create workspace folder: {exc}",
        ) from exc


@router.put(
    "/{user_id}/workspace-settings/projects/{project_id}/links",
    response_model=WorkspaceProjectResponse,
)
async def put_workspace_project_links(
    user_id: str,
    project_id: str,
    body: WorkspaceProjectLinksUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceProjectResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return _project_response(
            replace_workspace_project_links(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                project_id=project_id,
                organization_ids=body.organization_ids,
                now=now_utc_iso(),
            )
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update workspace project links: {exc}",
        ) from exc


@router.put(
    "/{user_id}/workspace-settings/folders/{folder_id}/links",
    response_model=WorkspaceFolderResponse,
)
async def put_workspace_folder_links(
    user_id: str,
    folder_id: str,
    body: WorkspaceFolderLinksUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceFolderResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return _folder_response(
            replace_workspace_folder_links(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                folder_id=folder_id,
                organization_ids=body.organization_ids,
                project_ids=body.project_ids,
                now=now_utc_iso(),
            )
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update workspace folder links: {exc}",
        ) from exc

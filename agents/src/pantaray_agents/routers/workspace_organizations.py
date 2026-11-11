from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AfterValidator, BaseModel, Field

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.storage.migrations import MigrationError
from pantaray_agents.local_runtime.tooling.repository import (
    WorkspaceOrganization,
    create_workspace_organization,
    delete_workspace_organization,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_organization_icons import (
    load_workspace_organization_icons,
    update_workspace_organization,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings_models import (
    WorkspaceOrganizationNameTakenError,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Workspace Settings"])

# The app resizes a picked image to a small icon before sending it.
MAX_ORGANIZATION_ICON_BYTES = 64 * 1024


def _sniff_image_media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def normalize_organization_icon(value: str) -> str:
    """Accepts a base64 data URL of a small PNG, JPEG or WebP image whose bytes match it."""
    # Base64 is 4/3 of the bytes; anything twice the cap is refused before decoding.
    if len(value) > 2 * MAX_ORGANIZATION_ICON_BYTES:
        raise ValueError(f"icon must be at most {MAX_ORGANIZATION_ICON_BYTES} bytes")
    header, separator, payload = value.partition(",")
    media_type = header.removeprefix("data:").removesuffix(";base64")
    if (
        not separator
        or header != f"data:{media_type};base64"
        or media_type not in ("image/png", "image/jpeg", "image/webp")
    ):
        raise ValueError("icon must be a base64 data URL of a PNG, JPEG or WebP image")
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error as exc:
        raise ValueError("icon data is not valid base64") from exc
    if len(data) > MAX_ORGANIZATION_ICON_BYTES:
        raise ValueError(f"icon must be at most {MAX_ORGANIZATION_ICON_BYTES} bytes")
    if _sniff_image_media_type(data) != media_type:
        raise ValueError(f"icon data is not a {media_type} image")
    return f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"


OrganizationIcon = Annotated[str, AfterValidator(normalize_organization_icon)]


class WorkspaceOrganizationResponse(BaseModel):
    organization_id: str
    display_name: str
    icon_data_url: str | None


class WorkspaceOrganizationCreateRequest(BaseModel):
    display_name: str = Field(min_length=1)
    icon_data_url: OrganizationIcon | None = None


class WorkspaceOrganizationUpdateRequest(BaseModel):
    """The organization's whole editable state: a null icon clears it."""

    display_name: str = Field(min_length=1)
    icon_data_url: OrganizationIcon | None


def _assert_user_allowed(*, user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="user_id mismatch",
        )


def organization_response(
    organization: WorkspaceOrganization,
    icon_data_url: str | None,
) -> WorkspaceOrganizationResponse:
    return WorkspaceOrganizationResponse(
        organization_id=organization.organization_id,
        display_name=organization.display_name,
        icon_data_url=icon_data_url,
    )


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
        organization = create_workspace_organization(
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            user_id=user_id,
            display_name=body.display_name,
            icon_data_url=body.icon_data_url,
            now=now_utc_iso(),
        )
        # A taken name answers with that organization, whose icon may differ from this one.
        icons = load_workspace_organization_icons(
            db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
        )
        return organization_response(
            organization, icons.get(organization.organization_id)
        )
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create workspace organization: {exc}",
        ) from exc


@router.put(
    "/{user_id}/workspace-settings/organizations/{organization_id}",
    response_model=WorkspaceOrganizationResponse,
)
async def put_workspace_organization(
    user_id: str,
    organization_id: str,
    body: WorkspaceOrganizationUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> WorkspaceOrganizationResponse:
    _assert_user_allowed(user_id=user_id, resolved_user_id=resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    try:
        return organization_response(
            update_workspace_organization(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                user_id=user_id,
                organization_id=organization_id,
                display_name=body.display_name,
                icon_data_url=body.icon_data_url,
                now=now_utc_iso(),
            ),
            body.icon_data_url,
        )
    except WorkspaceOrganizationNameTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Failed to update workspace organization: {exc}",
        ) from exc
    except MigrationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to update workspace organization: {exc}",
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

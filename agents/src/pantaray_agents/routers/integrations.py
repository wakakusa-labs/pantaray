"""The settings screen's API for connected external apps, account by account."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.integrations.contracts import IntegrationGateway
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_gateway,
    get_integration_tool_index,
)
from pantaray_agents.local_runtime.integrations.settings import (
    AccountStatus,
    IntegrationAccessNotAllowedError,
    IntegrationsStatus,
    IntegrationToolSetting,
    PermissionStore,
    UnknownIntegrationAccountError,
    UnknownIntegrationToolError,
    list_account_tools,
    load_integrations_status,
    set_account_access,
    set_tool_blocked,
)
from pantaray_agents.local_runtime.runtime.bootstrap import read_local_runtime_db_config
from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Integrations"])

_ACCOUNT_PATH = "/{user_id}/integrations/{toolkit}/accounts/{account}"


class AccountAccessUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: IntegrationAccess


def _store(user_id: str, resolved_user_id: str) -> PermissionStore:
    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return PermissionStore(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )


def _configured_gateway() -> IntegrationGateway:
    gateway = get_integration_gateway()
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="integrations are not configured",
        )
    return gateway


@router.get("/{user_id}/integrations", response_model=IntegrationsStatus)
async def get_integrations_status(
    user_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> IntegrationsStatus:
    store = _store(user_id, resolved_user_id)
    return await load_integrations_status(
        gateway=get_integration_gateway(),
        tool_index=get_integration_tool_index(),
        store=store,
    )


@router.put(f"{_ACCOUNT_PATH}/access", response_model=AccountStatus)
async def update_account_access(
    user_id: str,
    toolkit: str,
    account: str,
    body: AccountAccessUpdateRequest,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> AccountStatus:
    store = _store(user_id, resolved_user_id)
    try:
        return await set_account_access(
            gateway=_configured_gateway(),
            tool_index=get_integration_tool_index(),
            store=store,
            toolkit=toolkit,
            account=account,
            access=body.access,
            updated_at=now_utc_iso(),
        )
    except UnknownIntegrationAccountError as exc:
        raise _not_found("account", exc) from exc
    except IntegrationAccessNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": exc.reason.model_dump(mode="json")},
        ) from exc


@router.get(f"{_ACCOUNT_PATH}/tools", response_model=list[IntegrationToolSetting])
async def get_account_tools(
    user_id: str,
    toolkit: str,
    account: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> list[IntegrationToolSetting]:
    store = _store(user_id, resolved_user_id)
    try:
        return await list_account_tools(
            gateway=_configured_gateway(),
            tool_index=get_integration_tool_index(),
            store=store,
            toolkit=toolkit,
            account=account,
        )
    except UnknownIntegrationAccountError as exc:
        raise _not_found("account", exc) from exc


@router.put(
    f"{_ACCOUNT_PATH}/tools/{{slug}}/block", status_code=status.HTTP_204_NO_CONTENT
)
async def block_account_tool(
    user_id: str,
    toolkit: str,
    account: str,
    slug: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    await _set_blocked(user_id, resolved_user_id, toolkit, account, slug, True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    f"{_ACCOUNT_PATH}/tools/{{slug}}/block", status_code=status.HTTP_204_NO_CONTENT
)
async def unblock_account_tool(
    user_id: str,
    toolkit: str,
    account: str,
    slug: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    await _set_blocked(user_id, resolved_user_id, toolkit, account, slug, False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _set_blocked(
    user_id: str,
    resolved_user_id: str,
    toolkit: str,
    account: str,
    slug: str,
    blocked: bool,
) -> None:
    store = _store(user_id, resolved_user_id)
    try:
        await set_tool_blocked(
            gateway=_configured_gateway(),
            tool_index=get_integration_tool_index(),
            store=store,
            toolkit=toolkit,
            account=account,
            slug=slug,
            blocked=blocked,
            updated_at=now_utc_iso(),
        )
    except UnknownIntegrationAccountError as exc:
        raise _not_found("account", exc) from exc
    except UnknownIntegrationToolError as exc:
        raise _not_found("tool", exc) from exc


def _not_found(what: str, exc: LookupError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown {what}: {exc}"
    )

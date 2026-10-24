"""The settings screen's API for connected external apps, account by account."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict

from pantaray_agents.auth_http import get_current_user_id_from_token
from pantaray_agents.local_runtime.integrations.composio.client import (
    ComposioClientClosedError,
    ComposioRequestError,
)
from pantaray_agents.local_runtime.integrations.composio.connect import (
    ComposioConnections,
    ConnectionState,
    ConnectLink,
    ConnectNotAllowedError,
    UnknownConnectionError,
    UnknownToolkitError,
)
from pantaray_agents.local_runtime.integrations.composio.gateway import (
    ComposioGateway,
)
from pantaray_agents.local_runtime.integrations.composio.limits import (
    warn_composio_failure,
)
from pantaray_agents.local_runtime.integrations.contracts import IntegrationGateway
from pantaray_agents.local_runtime.integrations.registry import (
    get_integration_gateway,
    get_integration_tool_index,
)
from pantaray_agents.local_runtime.integrations.settings import (
    AccountStatus,
    IntegrationAccessNotAllowedError,
    IntegrationsKeyRejected,
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
from pantaray_agents.local_runtime.tooling.models import InternalStrictModel
from pantaray_agents.local_runtime.tooling.repository.integration_permissions import (
    IntegrationAccess,
    revoke_integration_account,
)

router = APIRouter(prefix="/v1/agents/users", tags=["Integrations"])

_ACCOUNT_PATH = "/{user_id}/integrations/{toolkit}/accounts/{account}"


class AccountAccessUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: IntegrationAccess


class ConnectionStateResponse(InternalStrictModel):
    state: ConnectionState


def _check_user(user_id: str, resolved_user_id: str) -> None:
    if resolved_user_id and user_id and resolved_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="user_id mismatch"
        )


def _store(user_id: str, resolved_user_id: str) -> PermissionStore:
    _check_user(user_id, resolved_user_id)
    db_path, busy_timeout_ms = read_local_runtime_db_config()
    return PermissionStore(
        db_path=db_path, busy_timeout_ms=busy_timeout_ms, user_id=user_id
    )


def _configured_gateway() -> IntegrationGateway:
    gateway = get_integration_gateway()
    if gateway is None:
        raise _not_configured()
    return gateway


def _connections() -> ComposioConnections:
    # Connecting is the connection method's own screen flow; only Composio has one.
    gateway = get_integration_gateway()
    if not isinstance(gateway, ComposioGateway):
        raise _not_configured()
    return gateway.connections


@router.get("/{user_id}/integrations", response_model=IntegrationsStatus)
async def get_integrations_status(
    user_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> IntegrationsStatus:
    store = _store(user_id, resolved_user_id)
    try:
        return await load_integrations_status(
            gateway=get_integration_gateway(),
            tool_index=get_integration_tool_index(),
            store=store,
        )
    except ComposioRequestError as exc:
        # 401: the key was deleted or regenerated, or is not a project key. The
        # user can fix only this failure, so only this one is told apart.
        if exc.response.status != status.HTTP_401_UNAUTHORIZED:
            raise
        return IntegrationsKeyRejected()


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


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="integrations are not configured",
    )


@router.post("/{user_id}/integrations/{toolkit}/connect", response_model=ConnectLink)
async def start_connect(
    user_id: str,
    toolkit: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ConnectLink:
    store = _store(user_id, resolved_user_id)
    connections = _connections()
    with _connect_errors():
        return await connections.connect(toolkit, store=store)


@router.post(f"{_ACCOUNT_PATH}/reconnect", response_model=ConnectLink)
async def start_reconnect(
    user_id: str,
    toolkit: str,
    account: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ConnectLink:
    store = _store(user_id, resolved_user_id)
    connections = _connections()
    with _connect_errors():
        return await connections.reconnect(toolkit, account, store=store)


@router.delete(_ACCOUNT_PATH, status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_account(
    user_id: str,
    toolkit: str,
    account: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> Response:
    store = _store(user_id, resolved_user_id)
    connections = _connections()
    # Composio first: when it fails the account stays listed with its
    # permissions, and disconnecting again picks up where this stopped.
    with _connect_errors():
        await connections.disconnect(toolkit, account)
    revoke_integration_account(
        db_path=store.db_path,
        busy_timeout_ms=store.busy_timeout_ms,
        user_id=store.user_id,
        toolkit=toolkit,
        account=account,
        revoked_at=now_utc_iso(),
    )
    try:
        await connections.refresh_after_disconnect(account)
    except (
        ComposioRequestError,
        ComposioClientClosedError,
        httpx2.TransportError,
        TimeoutError,
    ) as exc:
        # The account is gone at Composio and here; the next account read,
        # already marked stale, catches the caches up.
        warn_composio_failure("Composio accounts unavailable after disconnect", exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{user_id}/integrations/connections/{connection_id}",
    response_model=ConnectionStateResponse,
)
async def get_connection_state(
    user_id: str,
    connection_id: str,
    resolved_user_id: str = Depends(get_current_user_id_from_token),
) -> ConnectionStateResponse:
    _check_user(user_id, resolved_user_id)
    connections = _connections()
    with _connect_errors():
        state = await connections.status(connection_id)
    return ConnectionStateResponse(state=state)


@contextmanager
def _connect_errors() -> Iterator[None]:
    try:
        yield
    except ConnectNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": exc.reason.model_dump(mode="json")},
        ) from exc
    except UnknownToolkitError as exc:
        raise _not_found("toolkit", exc) from exc
    except UnknownConnectionError as exc:
        raise _not_found("connection", exc) from exc
    except ComposioClientClosedError as exc:
        # The key was removed while the request waited.
        raise _not_configured() from exc
    except (ComposioRequestError, httpx2.TransportError, TimeoutError) as exc:
        warn_composio_failure("Composio connect flow failed", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the integration service did not answer as expected",
        ) from exc

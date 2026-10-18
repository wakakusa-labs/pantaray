"""Connecting, reconnecting and disconnecting accounts through Composio.

Every connect and reconnect opens a new link, and in multi-account mode each
link creates its own connected account, with no alias needed (seen on
2026-10-18). A reconnect's new account replaces the old one once it is active,
so an abandoned reconnect leaves the old account as it was. Composio's refresh
route would keep the ID but is deprecated and turns the old account into an
abandoned link when the user walks away.

A link's URL carries a one-time token that completes the connection, so it is
handled like a credential: it goes back to the caller and nowhere else, never
into a log line or an exception's text.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from pantaray_agents.local_runtime.tooling.models import InternalStrictModel
from pantaray_agents.utils.timestamps import format_iso8601_utc_z_milliseconds

from ..contracts import ApiDenial, ToolkitSlug
from . import client as composio_client
from .client import (
    ACCOUNTS_TIMEOUT,
    METADATA_TIMEOUT,
    ComposioClient,
    ComposioRequestError,
    ComposioResponse,
)
from .toolkits import TOOLKITS_BY_SLUG

if TYPE_CHECKING:
    from .gateway import ComposioGateway

logger = logging.getLogger(__name__)

# Composio's documented lifetime of a hosted connect link; the link reply
# carries no expiry (seen on 2026-10-18).
LINK_LIFETIME: Final = timedelta(minutes=10)
# Composio's connected account IDs ('ca_P1GfI5U9cREh'); anything else is not
# sent to Composio.
_CONNECTION_ID: Final = re.compile(r"ca_[A-Za-z0-9_-]+")
# /revoke answers 400 when the toolkit cannot be revoked programmatically and
# 409 when the connection is not ACTIVE (code 623, seen on 2026-10-18).
_REVOKE_UNSUPPORTED: Final = 400
_REVOKE_NOT_REVOKABLE: Final = 409

type ConnectionState = Literal["pending", "active", "failed", "expired"]
type _Status = Literal[
    "INITIALIZING", "INITIATED", "ACTIVE", "FAILED", "EXPIRED", "INACTIVE", "REVOKED"
]

_STATES: Final[dict[_Status, ConnectionState]] = {
    "INITIALIZING": "pending",
    "INITIATED": "pending",
    "ACTIVE": "active",
    "FAILED": "failed",
    "EXPIRED": "expired",
    # Disabled or revoked while the user was away: waiting will not activate it.
    "INACTIVE": "failed",
    "REVOKED": "failed",
}


class ConnectLink(InternalStrictModel):
    redirect_url: str
    expires_at: str
    connection_id: str


@dataclass(frozen=True, slots=True)
class Reconnect:
    # The account this link's account replaces once active.
    replaces: str


# Where a link came from, as far as this process knows: None after a restart,
# or for a connection this process did not link.
type LinkOrigin = Literal["connect"] | Reconnect | None


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    state: ConnectionState
    toolkit: ToolkitSlug
    account: str
    origin: LinkOrigin


class ConnectNotAllowedError(Exception):
    def __init__(self, reason: ApiDenial) -> None:
        super().__init__(f"the key does not allow connecting: {reason}")
        self.reason = reason


class UnknownToolkitError(LookupError):
    pass


class UnknownConnectionError(LookupError):
    pass


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _LinkReply(_Reply):
    connected_account_id: str
    redirect_url: str


class _ToolkitRef(_Reply):
    slug: str


class _ConnectionReply(_Reply):
    id: str
    user_id: str
    status: _Status
    toolkit: _ToolkitRef


class ComposioConnections:
    """One gateway's connect flow: links, reconnects, disconnects, activation."""

    def __init__(self, gateway: ComposioGateway, client: ComposioClient) -> None:
        self._gateway = gateway
        self._client = client
        self._lock = threading.Lock()
        # Links this process opened and not yet settled. Not persisted: after
        # a restart a link's origin is unknown, so its account gets no setting
        # and an old account it would have replaced stays for the user to
        # disconnect.
        self._links: dict[str, Literal["connect"] | Reconnect] = {}
        # Activations run or running, so overlapping polls do the work once.
        self._activations: dict[str, concurrent.futures.Future[None]] = {}

    async def connect(self, toolkit: ToolkitSlug) -> ConnectLink:
        """A link for one more account of ``toolkit``."""

        self._check_can_connect(toolkit)
        return await self._open_link(toolkit, "connect")

    async def reconnect(self, toolkit: ToolkitSlug, account: str) -> ConnectLink:
        """A link whose account replaces ``account`` once the user consents."""

        self._check_can_connect(toolkit)
        await self._find(toolkit, account)
        return await self._open_link(toolkit, Reconnect(replaces=account))

    async def disconnect(self, toolkit: ToolkitSlug, account: str) -> None:
        """Revoke the grant at the provider, then delete the connected account.

        A failed revoke deletes nothing, so the account stays listed and the
        user can try again; a revoke that already happened answers 409.
        """

        await self._find(toolkit, account)
        revoked = await self._client.send(
            "POST", f"/connected_accounts/{account}/revoke", timeout=METADATA_TIMEOUT
        )
        if revoked.status == _REVOKE_UNSUPPORTED:
            # Composio cannot revoke this toolkit's grant; the user can at the
            # provider. Keeping the account would not revoke it either.
            logger.warning(
                "Composio cannot revoke at the provider", extra={"toolkit": toolkit}
            )
        elif revoked.status not in (200, 404, _REVOKE_NOT_REVOKABLE):
            raise ComposioRequestError(revoked)
        await self._delete(account)

    async def refresh_after_disconnect(self, account: str) -> None:
        await self._gateway.refresh_accounts(account, listed=False)

    async def status(self, connection_id: str) -> ConnectionStatus:
        """The connection's state. A link of this process that failed or
        expired is deleted: it never became an account."""

        if not _CONNECTION_ID.fullmatch(connection_id):
            raise UnknownConnectionError(connection_id)
        response = await self._client.send(
            "GET", f"/connected_accounts/{connection_id}", timeout=ACCOUNTS_TIMEOUT
        )
        if response.status == 404:
            raise UnknownConnectionError(connection_id)
        reply = _parse(_ConnectionReply, response, ok=(200,))
        if (
            reply.id != connection_id
            or reply.user_id != composio_client.USER_ID
            or reply.toolkit.slug not in TOOLKITS_BY_SLUG
        ):
            # Not a connection this desktop made.
            raise UnknownConnectionError(connection_id)
        state = _STATES[reply.status]
        with self._lock:
            origin = self._links.get(connection_id)
        if state in ("failed", "expired") and origin is not None:
            await self._delete(connection_id)
            with self._lock:
                self._links.pop(connection_id, None)
        return ConnectionStatus(
            state=state,
            toolkit=reply.toolkit.slug,
            account=connection_id,
            origin=origin,
        )

    async def activate(self, found: ConnectionStatus) -> None:
        """Once per connection: delete the account it replaces, refresh the caches.

        Polls that overlap wait for the one doing it; a failure lets the next
        poll try again.
        """

        with self._lock:
            pending = self._activations.get(found.account)
            owner = pending is None
            if pending is None:
                pending = self._activations[found.account] = concurrent.futures.Future()
        if not owner:
            # Shielded: a poll that gives up must not cancel the shared work.
            await asyncio.shield(asyncio.wrap_future(pending))
            return
        try:
            if isinstance(found.origin, Reconnect):
                # Deleted without a revoke: the provider may hold one grant
                # for both connections, and revoking it would cut the new one.
                await self._delete(found.origin.replaces)
            await self._gateway.refresh_accounts(found.account, listed=True)
        except BaseException as exc:
            with self._lock:
                del self._activations[found.account]
            pending.set_exception(exc)
            raise
        with self._lock:
            self._links.pop(found.account, None)
        pending.set_result(None)

    async def _open_link(
        self, toolkit: ToolkitSlug, origin: Literal["connect"] | Reconnect
    ) -> ConnectLink:
        session_id = await self._gateway.session_with(toolkit)
        if isinstance(session_id, ComposioResponse):
            raise ComposioRequestError(session_id)
        response = await self._client.send(
            "POST",
            f"/tool_router/session/{session_id}/link",
            json={"toolkit": toolkit},
            timeout=METADATA_TIMEOUT,
        )
        reply = _parse(_LinkReply, response, ok=(200, 201))
        with self._lock:
            self._links[reply.connected_account_id] = origin
        expires_at = datetime.now(UTC) + LINK_LIFETIME
        return ConnectLink(
            redirect_url=reply.redirect_url,
            expires_at=format_iso8601_utc_z_milliseconds(expires_at),
            connection_id=reply.connected_account_id,
        )

    async def _delete(self, account: str) -> None:
        deleted = await self._client.send(
            "DELETE", f"/connected_accounts/{account}", timeout=METADATA_TIMEOUT
        )
        if deleted.status not in (200, 404):
            raise ComposioRequestError(deleted)

    def _check_can_connect(self, toolkit: ToolkitSlug) -> None:
        if toolkit not in TOOLKITS_BY_SLUG:
            raise UnknownToolkitError(toolkit)
        limits = self._gateway.limits()
        if not limits.can_connect:
            raise ConnectNotAllowedError(
                next(r for r in limits.reasons if r.capability == "connect")
            )

    async def _find(self, toolkit: ToolkitSlug, account: str) -> None:
        if toolkit not in TOOLKITS_BY_SLUG:
            raise UnknownToolkitError(toolkit)
        accounts = await self._gateway.list_accounts(toolkit)
        if all(found.account != account for found in accounts):
            raise UnknownConnectionError(f"{toolkit}/{account}")


def _parse[ReplyT: _Reply](
    model: type[ReplyT], response: ComposioResponse, *, ok: tuple[int, ...]
) -> ReplyT:
    if response.status not in ok:
        raise ComposioRequestError(response)
    try:
        return model.model_validate(response.body)
    except ValidationError:
        # The validation text quotes the body, which can hold the link.
        raise ComposioRequestError(response) from None

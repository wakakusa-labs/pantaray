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
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from pantaray_agents.local_runtime.tooling.models import InternalStrictModel
from pantaray_agents.utils.timestamps import format_iso8601_utc_z_milliseconds

from ..contracts import ApiDenial, ToolkitSlug
from ..settings import (
    PermissionStore,
    apply_new_account_default,
    carry_over_account_setting,
)
from . import client as composio_client
from .client import (
    ACCOUNTS_TIMEOUT,
    METADATA_TIMEOUT,
    ComposioClient,
    ComposioClientClosedError,
    ComposioRequestError,
    ComposioResponse,
)
from .limits import warn_composio_failure
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


@dataclass(frozen=True, slots=True)
class _PendingLink:
    toolkit: ToolkitSlug
    origin: Literal["connect"] | Reconnect
    # Where the setting of the link's account is saved once it is active.
    store: PermissionStore


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
        # Links this process opened and not yet finalized. Not persisted: after
        # a restart a link's origin is unknown, so its account gets no setting
        # and an old account it would have replaced stays for the user to
        # disconnect.
        self._links: dict[str, _PendingLink] = {}
        # Finalizations run or running, so every observer does the work once.
        self._finalizations: dict[str, concurrent.futures.Future[None]] = {}

    async def connect(
        self, toolkit: ToolkitSlug, *, store: PermissionStore
    ) -> ConnectLink:
        """A link for one more account of ``toolkit``."""

        self._check_can_connect(toolkit)
        return await self._open_link(_PendingLink(toolkit, "connect", store))

    async def reconnect(
        self, toolkit: ToolkitSlug, account: str, *, store: PermissionStore
    ) -> ConnectLink:
        """A link whose account replaces ``account`` once the user consents."""

        self._check_can_connect(toolkit)
        await self._find(toolkit, account)
        return await self._open_link(
            _PendingLink(toolkit, Reconnect(replaces=account), store)
        )

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
        await self._gateway.refresh_accounts(unlisted=frozenset({account}))

    async def status(self, connection_id: str) -> ConnectionState:
        """The connection's state, finalized first when active. A link of this
        process that failed or expired is deleted: it never became an account."""

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
        if state == "active":
            await self.finalize(connection_id)
            return state
        with self._lock:
            ours = connection_id in self._links
        if state in ("failed", "expired") and ours:
            await self._delete(connection_id)
            with self._lock:
                self._links.pop(connection_id, None)
        return state

    def finalize_soon(self, active: Iterable[str]) -> None:
        """Finalize, on a thread of its own, this process's links the account
        read found active, so a connection settles without a poll.

        Never awaited by that read: finalizing reads the accounts again, and
        that read may be the one in flight.
        """

        with self._lock:
            due = [
                account
                for account in active
                if account in self._links and account not in self._finalizations
            ]
        if due:
            threading.Thread(
                target=self._run_finalize,
                args=(due,),
                name="composio-finalize",
                daemon=True,
            ).start()

    def _run_finalize(self, accounts: list[str]) -> None:
        for account in accounts:
            try:
                asyncio.run(self.finalize(account))
            except (
                ComposioRequestError,
                ComposioClientClosedError,
                httpx2.TransportError,
                TimeoutError,
                sqlite3.Error,
            ) as exc:
                # The next account read that finds it active tries again.
                warn_composio_failure(
                    "Composio connection not finalized", exc, account=account
                )

    async def finalize(self, account: str) -> None:
        """Once per active connection: save the setting of a link this process
        opened, delete the account a reconnect replaces, refresh the caches.

        Observers that overlap wait for the one doing it; a failure lets the
        next one try again. A connection of unknown origin gets no setting.
        """

        with self._lock:
            pending = self._finalizations.get(account)
            owner = pending is None
            if pending is None:
                pending = self._finalizations[account] = concurrent.futures.Future()
            link = self._links.get(account)
        if not owner:
            # Shielded: a poll that gives up must not cancel the shared work.
            await asyncio.shield(asyncio.wrap_future(pending))
            return
        try:
            replaced = await self._settle(account, link)
            await self._gateway.refresh_accounts(
                listed=frozenset({account}), unlisted=replaced
            )
        except BaseException as exc:
            with self._lock:
                del self._finalizations[account]
            pending.set_exception(exc)
            raise
        with self._lock:
            self._links.pop(account, None)
        pending.set_result(None)

    async def _settle(self, account: str, link: _PendingLink | None) -> frozenset[str]:
        """Save the account's setting; returns the account it replaced.

        The setting is saved before the replaced account is deleted, and both
        writes are idempotent, so a failure in between loses nothing.
        """

        if link is None:
            return frozenset()
        updated_at = format_iso8601_utc_z_milliseconds(datetime.now(UTC))
        if link.origin == "connect":
            apply_new_account_default(
                store=link.store,
                toolkit=link.toolkit,
                account=account,
                updated_at=updated_at,
            )
            return frozenset()
        carry_over_account_setting(
            store=link.store,
            toolkit=link.toolkit,
            replaced=link.origin.replaces,
            account=account,
            updated_at=updated_at,
        )
        # Deleted without a revoke: the provider may hold one grant for both
        # connections, and revoking it would cut the new one.
        await self._delete(link.origin.replaces)
        return frozenset({link.origin.replaces})

    async def _open_link(self, link: _PendingLink) -> ConnectLink:
        session_id = await self._gateway.session_with(link.toolkit)
        if isinstance(session_id, ComposioResponse):
            raise ComposioRequestError(session_id)
        response = await self._client.send(
            "POST",
            f"/tool_router/session/{session_id}/link",
            json={"toolkit": link.toolkit},
            timeout=METADATA_TIMEOUT,
        )
        reply = _parse(_LinkReply, response, ok=(200, 201))
        with self._lock:
            self._links[reply.connected_account_id] = link
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

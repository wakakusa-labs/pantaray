"""The integration gateway over Composio: accounts, tool lists, key limits, calls.

Every tool call runs through one Tool Router session with Composio's own meta
tools off, so the session is only the execution path; Pantaray searches its own
index and connects from its own screen. Callers run on several threads, each
with its own event loop, so the cached state is replaced whole and the one
shared account read is a thread-safe future, never an asyncio primitive.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Final, Literal

import httpx2
from pydantic import ValidationError

from pantaray_agents.schema.agent.base import JSONValue

from ..contracts import (
    ApiDenial,
    GatewayLimits,
    IntegrationAccount,
    IntegrationError,
    IntegrationErrorKind,
    IntegrationHealth,
    IntegrationTool,
    ToolClass,
    ToolFailed,
    ToolkitEntry,
    ToolkitSlug,
    ToolOutcome,
)
from ..registry import (
    get_integration_gateway,
    get_integration_tool_index,
    set_integration_gateway,
)
from . import client as composio_client
from .client import (
    ACCOUNTS_TIMEOUT,
    EXECUTE_TIMEOUT,
    METADATA_TIMEOUT,
    ComposioClient,
    ComposioClientClosedError,
    ComposioRequestError,
    ComposioResponse,
)
from .mapping import (
    CODE_KEY_PERMISSION_DENIED,
    CODE_NO_ACTIVE_CONNECTION,
    CODE_SESSION_NOT_FOUND,
    CODE_TOOLKIT_NOT_IN_SESSION,
    CODE_VALIDATION_ERROR,
    PENDING_STATUSES,
    RawAccount,
    RawTool,
    ScopeGroups,
    account_health,
    account_label,
    api_failure,
    execute_outcome,
    granted_scopes,
    integration_tool,
    missing_scope,
    required_scopes,
    tool_disposition,
)
from .toolkits import TOOLKITS, TOOLKITS_BY_SLUG

logger = logging.getLogger(__name__)

# Every Action turn and every Insight probe asks for the accounts; connecting
# and a refused call drop the cache at once, so this bounds only outside changes.
ACCOUNTS_TTL_SECONDS: Final = 60.0
# The whole account read, every page; below the Action turn's 5 s budget.
ACCOUNTS_FETCH_SECONDS: Final = 4.0
# Composio checks a key's permission before the body, so an empty body to an
# area's write route answers 403/812 when denied and 400/10400 when allowed,
# creating nothing (verified on 2026-10-18; no route reports a key's permissions).
_PROBE_SESSION: Final = "trs_pantaray_probe"
_LIMIT_PROBES: Final[tuple[tuple[Literal["connect", "execute"], str, str], ...]] = (
    ("connect", "auth_configs", "/auth_configs"),
    ("connect", "connected_accounts", "/connected_accounts/link"),
    ("connect", "session_management", f"/tool_router/session/{_PROBE_SESSION}/link"),
    ("execute", "session_management", "/tool_router/session"),
    (
        "execute",
        "session_tool_execution",
        f"/tool_router/session/{_PROBE_SESSION}/execute",
    ),
)
# Until a probe settles nothing says the key falls short; a real shortfall still
# answers 403/812 at the first call, which probes again.
_UNPROBED_LIMITS: Final = GatewayLimits(can_connect=True, can_execute=True, reasons=[])
# Refused before the tool ran, so asking once more cannot run a write twice.
_RENEW_SESSION_CODES: Final = (CODE_TOOLKIT_NOT_IN_SESSION, CODE_SESSION_NOT_FOUND)


@dataclass(frozen=True, slots=True)
class _Account:
    account: IntegrationAccount
    health: IntegrationHealth
    granted: frozenset[str] | None


@dataclass(frozen=True, slots=True)
class _Accounts:
    by_id: dict[str, _Account]
    fetched_at: float

    @property
    def toolkits(self) -> frozenset[ToolkitSlug]:
        return frozenset(item.account.toolkit for item in self.by_id.values())


@dataclass(frozen=True, slots=True)
class _ToolMeta:
    toolkit: ToolkitSlug
    tool_class: ToolClass
    scope_groups: ScopeGroups


@dataclass(frozen=True, slots=True)
class _Session:
    session_id: str
    toolkits: frozenset[ToolkitSlug]


class ComposioGateway:
    def __init__(self, client: ComposioClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._limits: GatewayLimits | None = None
        self._accounts: _Accounts | None = None
        self._tools: dict[str, _ToolMeta] = {}
        # Toolkits whose tools this gateway has put in the tool index.
        self._indexed: frozenset[ToolkitSlug] = frozenset()
        self._session: _Session | None = None
        # The one account read in flight, shared by callers on every thread.
        self._accounts_fetch: concurrent.futures.Future[_Accounts | None] | None = None
        # asyncio keeps only weak references to tasks.
        self._fetch_tasks: set[asyncio.Task[None]] = set()
        self._syncing = False

    def close(self) -> None:
        """The key was removed or replaced: no request not yet started is sent."""
        self._client.close()

    def toolkits(self) -> list[ToolkitEntry]:
        return list(TOOLKITS)

    async def list_accounts(self, toolkit: ToolkitSlug) -> list[IntegrationAccount]:
        accounts = await self._current_accounts()
        return [
            item.account
            for item in accounts.by_id.values()
            if item.account.toolkit == toolkit
        ]

    def health(self, account: str) -> IntegrationHealth:
        snapshot = self._accounts
        found = snapshot.by_id.get(account) if snapshot else None
        return found.health if found else "not_connected"

    def limits(self) -> GatewayLimits:
        return self._limits or _UNPROBED_LIMITS

    async def list_tools(self, toolkit: ToolkitSlug) -> list[IntegrationTool]:
        entry = TOOLKITS_BY_SLUG[toolkit]
        items = await self._client.list_all(
            "/tools",
            {"toolkit_slug": toolkit, "include_deprecated": "false"},
            timeout=METADATA_TIMEOUT,
        )
        tools: list[IntegrationTool] = []
        metas: dict[str, _ToolMeta] = {}
        for item in items:
            try:
                raw = RawTool.model_validate(item)
            except ValidationError:
                logger.warning("Composio tool skipped", extra={"toolkit": toolkit})
                continue
            disposition = tool_disposition(raw, entry.class_overrides)
            if disposition == "blocked" or raw.is_deprecated:
                continue
            tools.append(integration_tool(raw, disposition))
            metas[raw.slug] = _ToolMeta(toolkit, disposition, required_scopes(raw))
        with self._lock:
            kept = {s: m for s, m in self._tools.items() if m.toolkit != toolkit}
            self._tools = kept | metas
        return tools

    def api_denied_reason(self, *, slug: str, account: str) -> ApiDenial | None:
        meta = self._tools.get(slug)
        snapshot = self._accounts
        found = snapshot.by_id.get(account) if snapshot else None
        if meta is None or found is None:
            return None
        missing = missing_scope(meta.scope_groups, found.granted)
        if missing is None:
            return None
        return ApiDenial(source="account_grant", capability="tool", detail=missing)

    async def execute(
        self,
        *,
        slug: str,
        account: str,
        arguments: dict[str, JSONValue],
        tool_request_id: str,
    ) -> ToolOutcome:
        # Composio takes no idempotency key, so tool_request_id has no use here.
        try:
            return await self._execute(slug=slug, account=account, arguments=arguments)
        except ComposioClientClosedError:
            # The key went away while the call waited; nothing was sent.
            return _failed("connection_missing")

    async def _execute(
        self, *, slug: str, account: str, arguments: dict[str, JSONValue]
    ) -> ToolOutcome:
        meta = self._tools.get(slug)
        is_write = meta is None or meta.tool_class == "write"
        try:
            accounts = await self._current_accounts()
        except (ComposioRequestError, httpx2.TransportError, TimeoutError) as exc:
            _warn("Composio accounts unavailable", exc)
            return _failed("upstream_failed")
        if account not in accounts.by_id:
            # Composio would answer 4300; nothing to send.
            return _failed("connection_missing")
        body: JSONValue = {
            "tool_slug": slug,
            "arguments": arguments,
            "account": account,
        }
        result = await self._send(
            body, accounts.toolkits, is_write=is_write, renew=False
        )
        if isinstance(result, ComposioResponse) and result.code in _RENEW_SESSION_CODES:
            result = await self._send(
                body, accounts.toolkits, is_write=is_write, renew=True
            )
        if isinstance(result, ToolFailed):
            return result
        if result.status != 200:
            return await self._refused(result, slug=slug, is_write=is_write)
        outcome = execute_outcome(result.body, is_write=is_write)
        if isinstance(outcome, ToolFailed):
            logger.warning(
                "Composio tool failed", extra={"slug": slug, "kind": outcome.error.kind}
            )
        return outcome

    def invalidate_accounts(self) -> None:
        """Make the next account read ask Composio (a connection was added or changed).

        health and api_denied_reason keep answering from the last read until then.
        """
        snapshot = self._accounts
        if snapshot is not None:
            self._accounts = replace(snapshot, fetched_at=float("-inf"))

    async def sync(self) -> None:
        """Probe the key's limits if unknown, read the accounts, fill the tool index.

        Raises what the account read raises; a toolkit whose tool list fails is
        tried at the next sync.
        """

        if self._limits is None:
            await self._probe_limits()
        accounts = await self._fetch_accounts()
        await self._sync_index(accounts.toolkits)

    def start_sync(self) -> None:
        """Run sync on its own thread, once at a time, without waiting for it."""
        with self._lock:
            if self._syncing:
                return
            self._syncing = True
        threading.Thread(
            target=self._run_sync, name="composio-sync", daemon=True
        ).start()

    def _run_sync(self) -> None:
        try:
            asyncio.run(self.sync())
        except (
            ComposioRequestError,
            ComposioClientClosedError,
            httpx2.TransportError,
            TimeoutError,
        ) as exc:
            # The next account read that finds the index behind syncs again.
            _warn("Composio sync failed", exc)
        finally:
            with self._lock:
                self._syncing = False

    async def _current_accounts(self) -> _Accounts:
        """The accounts only: never waits for the key probes or the tool lists."""

        snapshot = self._accounts
        if snapshot is not None and (
            time.monotonic() - snapshot.fetched_at < ACCOUNTS_TTL_SECONDS
        ):
            return snapshot
        snapshot = await self._fetch_accounts()
        behind = snapshot.toolkits != self._indexed or self._limits is None
        if behind and get_integration_gateway() is self:
            self.start_sync()
        return snapshot

    async def _fetch_accounts(self) -> _Accounts:
        while True:
            with self._lock:
                pending = self._accounts_fetch
                owner = pending is None
                if pending is None:
                    pending = self._accounts_fetch = concurrent.futures.Future()
            if owner:
                # A task, so a caller that gives up at its own deadline does not
                # abandon the read the other callers wait for.
                task = asyncio.get_running_loop().create_task(
                    self._run_accounts_fetch(pending)
                )
                self._fetch_tasks.add(task)
                task.add_done_callback(self._fetch_tasks.discard)
            snapshot = await asyncio.shield(asyncio.wrap_future(pending))
            if snapshot is not None:
                return snapshot
            # The loop that ran the read closed first; read again.

    async def _run_accounts_fetch(
        self, pending: concurrent.futures.Future[_Accounts | None]
    ) -> None:
        try:
            async with asyncio.timeout(ACCOUNTS_FETCH_SECONDS):
                snapshot = await self._load_accounts()
        except asyncio.CancelledError:
            self._end_fetch(pending)
            pending.set_result(None)
            raise
        except Exception as exc:  # noqa: BLE001
            # Handed to every waiter, which handles it as its own.
            self._end_fetch(pending)
            pending.set_exception(exc)
        else:
            self._end_fetch(pending)
            pending.set_result(snapshot)

    def _end_fetch(self, pending: concurrent.futures.Future[_Accounts | None]) -> None:
        # Cleared before the result is set, so a waiter that reads again starts
        # a new read instead of finding this one.
        with self._lock:
            if self._accounts_fetch is pending:
                self._accounts_fetch = None

    async def _load_accounts(self) -> _Accounts:
        items = await self._client.list_all(
            "/connected_accounts",
            {"user_ids": composio_client.USER_ID},
            timeout=ACCOUNTS_TIMEOUT,
        )
        by_id: dict[str, _Account] = {}
        for item in items:
            try:
                raw = RawAccount.model_validate(item)
            except ValidationError:
                logger.warning("Composio connected account skipped")
                continue
            if (
                raw.toolkit.slug not in TOOLKITS_BY_SLUG
                or raw.status in PENDING_STATUSES
            ):
                continue
            by_id[raw.id] = _Account(
                account=IntegrationAccount(
                    account=raw.id, toolkit=raw.toolkit.slug, label=account_label(raw)
                ),
                health=account_health(raw.status),
                granted=granted_scopes(raw),
            )
        snapshot = _Accounts(by_id=by_id, fetched_at=time.monotonic())
        self._accounts = snapshot
        return snapshot

    async def _sync_index(self, connected: frozenset[ToolkitSlug]) -> None:
        # Read the index before the check: once this gateway is replaced, the
        # index read here is the old one, which no caller sees any more.
        index = get_integration_tool_index()
        if get_integration_gateway() is not self:
            return
        for toolkit in sorted(connected - self._indexed):
            try:
                tools = await self.list_tools(toolkit)
            except (ComposioRequestError, httpx2.TransportError) as exc:
                _warn("Composio tool list failed", exc, toolkit=toolkit)
                continue
            index.replace(toolkit, tools)
            with self._lock:
                self._indexed = self._indexed | {toolkit}
        with self._lock:
            gone = self._indexed - connected
            self._indexed = self._indexed & connected
        for toolkit in gone:
            index.replace(toolkit, [])

    async def _probe_limits(self) -> None:
        denied: dict[Literal["connect", "execute"], list[str]] = {
            "connect": [],
            "execute": [],
        }
        for capability, area, path in _LIMIT_PROBES:
            try:
                response = await self._client.send(
                    "POST", path, json={}, timeout=METADATA_TIMEOUT
                )
            except httpx2.TransportError as exc:
                # Inconclusive: the next sync probes again.
                _warn("Composio key probe failed", exc)
                return
            if response.status == 401:
                self._limits = GatewayLimits(
                    can_connect=False,
                    can_execute=False,
                    reasons=[
                        ApiDenial(source="connection_method", capability="connect"),
                        ApiDenial(source="connection_method", capability="execute"),
                    ],
                )
                return
            if response.status == 403 and response.code == CODE_KEY_PERMISSION_DENIED:
                if area not in denied[capability]:
                    denied[capability].append(area)
            elif not (
                response.status == 400 and response.code == CODE_VALIDATION_ERROR
            ):
                _warn("Composio key probe failed", ComposioRequestError(response))
                return
        self._limits = GatewayLimits(
            can_connect=not denied["connect"],
            can_execute=not denied["execute"],
            reasons=[
                # The areas are Composio's identifiers, shown as they are.
                ApiDenial(
                    source="connection_method",
                    capability=capability,
                    detail=", ".join(areas),
                )
                for capability, areas in denied.items()
                if areas
            ],
        )

    async def _send(
        self,
        body: JSONValue,
        toolkits: frozenset[ToolkitSlug],
        *,
        is_write: bool,
        renew: bool,
    ) -> ComposioResponse | ToolFailed:
        try:
            session_id = await self._session_id(toolkits, renew=renew)
        except httpx2.TransportError as exc:
            _warn("Composio session failed", exc)
            return _failed("upstream_failed")
        if isinstance(session_id, ComposioResponse):
            # Refused before any tool ran.
            return await self._refused(session_id, slug=None, is_write=False)
        try:
            return await self._client.send(
                "POST",
                f"/tool_router/session/{session_id}/execute",
                json=body,
                timeout=EXECUTE_TIMEOUT,
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
            # The request never left this computer.
            _warn("Composio call failed", exc)
            return _failed("upstream_failed")
        except httpx2.TransportError as exc:
            _warn("Composio call failed", exc)
            return _failed("outcome_unknown" if is_write else "upstream_failed")

    async def _session_id(
        self, toolkits: frozenset[ToolkitSlug], *, renew: bool
    ) -> str | ComposioResponse:
        current = self._session
        if current is not None and current.toolkits == toolkits and not renew:
            return current.session_id
        response = await self._client.send(
            "POST",
            "/tool_router/session",
            json=_session_body(toolkits),
            timeout=METADATA_TIMEOUT,
        )
        body = response.body
        session_id = body.get("session_id") if isinstance(body, dict) else None
        if response.status not in (200, 201) or not isinstance(session_id, str):
            return response
        self._session = _Session(session_id=session_id, toolkits=toolkits)
        return session_id

    async def _refused(
        self, response: ComposioResponse, *, slug: str | None, is_write: bool
    ) -> ToolFailed:
        logger.warning(
            "Composio refused a call",
            extra={"slug": slug, "status": response.status, "code": response.code},
        )
        if response.status == 403 and response.code == CODE_KEY_PERMISSION_DENIED:
            # The key was replaced or its permissions changed.
            self._limits = None
            await self._probe_limits()
        if response.code == CODE_NO_ACTIVE_CONNECTION or response.status in (404, 422):
            # The account's state changed; the next read shows it.
            self.invalidate_accounts()
        return ToolFailed(error=api_failure(response, is_write=is_write))


def _session_body(toolkits: frozenset[ToolkitSlug]) -> JSONValue:
    enable: list[JSONValue] = [*sorted(toolkits)]
    return {
        "user_id": composio_client.USER_ID,
        "toolkits": {"enable": enable},
        # Pantaray connects from its own screen and searches its own index, so
        # Composio's sandbox, connection, search and multi-execute tools are off.
        "workbench": {"enable": False},
        "manage_connections": {"enable": False},
        "search": {"enable": False},
        "execute": {"enable_multi_execute": False},
        # Every call names its account, so two accounts of one toolkit never mix.
        "multi_account": {"enable": True, "require_explicit_selection": True},
    }


def _failed(kind: IntegrationErrorKind) -> ToolFailed:
    return ToolFailed(error=IntegrationError(kind=kind))


def _warn(message: str, exc: Exception, *, toolkit: str | None = None) -> None:
    # Only the status and code: Composio's text can quote part of the key.
    reason = (
        f"status={exc.response.status} code={exc.response.code}"
        if isinstance(exc, ComposioRequestError)
        else type(exc).__name__
    )
    logger.warning(message, extra={"reason": reason, "toolkit": toolkit})


def install_composio_gateway(api_key: str | None) -> None:
    """Register a gateway for the key, or none without one.

    The first sync runs on its own thread: Composio being slow or down must not
    hold up the control socket or the backend's start. The replaced gateway is
    closed first, so a call that already resolved it sends nothing.
    """

    previous = get_integration_gateway()
    if isinstance(previous, ComposioGateway):
        # A call already holding the old gateway must not send with the old key.
        previous.close()
    if api_key is None:
        set_integration_gateway(None)
        return
    gateway = ComposioGateway(ComposioClient(api_key))
    set_integration_gateway(gateway)
    gateway.start_sync()

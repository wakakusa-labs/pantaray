"""Loopback proxy that records where sandboxed commands connect.

Every network connection of a sandboxed command is meant to go through this
proxy: HTTP (CONNECT tunnels and plain ``http://`` forwarding) and SOCKS5 share
one port on 127.0.0.1. It records destination host, port, bytes and success per
command lease. It does not intercept TLS, record URLs or content, or prompt.

One proxy serves the whole process. Commands run on many threads, each with its
own event loop (one per Action or subagent job, one per chat turn), so the proxy
owns a daemon thread and loop of its own, started on the first lease.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import ipaddress
import secrets
import socket
import threading
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import astuple, dataclass, field
from typing import Literal

from . import egress_proxy_wire as wire
from .egress_proxy_relay import forward, tunnel

PROXY_HOST = "127.0.0.1"
# Bounds the request/handshake phase only; established tunnels have no timeout.
HANDSHAKE_TIMEOUT_S = 10.0
# Caps a request head (and a response head of a forwarded request).
HEAD_LIMIT_BYTES = 64 * 1024
# Process-wide cap on open client connections, each holding up to two FDs: it
# fits parallel downloaders (uv opens 50, npm up to 50) and keeps the proxy from
# exhausting the backend's descriptors.
MAX_OPEN_CONNECTIONS = 128
_START_TIMEOUT_S = 5.0
_TOKEN_BYTES = 32
# "This network" (RFC 1122): 0.0.0.0 reaches the local host on common stacks.
_THIS_NETWORK = ipaddress.IPv4Network("0.0.0.0/8")

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type _DialFailure = Literal["refused", "unreachable"]
type _Stream = tuple[asyncio.StreamReader, asyncio.StreamWriter]


class EgressProxyStartError(RuntimeError):
    """The proxy could not start; the command must not get network access."""


@dataclass(frozen=True, slots=True)
class NetworkDestination:
    """Connections one lease made to one (host, port), as the client named it.

    ``connections`` counts every attempt; ``failed`` those refused by policy or
    not reached. Bytes are what the proxy delivered to each side's socket.
    """

    host: str
    port: int
    connections: int
    failed: int
    bytes_sent: int
    bytes_received: int


@dataclass(slots=True)
class _Tally:
    connections: int = 0
    failed: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0

    def count_sent(self, size: int) -> None:
        self.bytes_sent += size

    def count_received(self, size: int) -> None:
        self.bytes_received += size


@dataclass(eq=False, slots=True)
class _LeaseState:
    # Counters are written only on the proxy thread; the proxy lock guards the
    # destinations dict, which lease holders read from other threads.
    destinations: dict[wire.ProxyTarget, _Tally] = field(default_factory=dict)
    # Authorized connections and their client and upstream writers. Touched
    # only on the proxy loop.
    connections: dict[asyncio.Task[object], list[asyncio.StreamWriter]] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class EgressLease:
    """Proxy URLs for one command and what it has connected to so far."""

    http_proxy_url: str = field(repr=False)
    socks_proxy_url: str = field(repr=False)
    _state: _LeaseState = field(repr=False)
    _lock: threading.Lock = field(repr=False)

    def destinations(self) -> tuple[NetworkDestination, ...]:
        """Per (host, port) in order of first use; complete once the lease ends."""

        with self._lock:
            return tuple(
                NetworkDestination(target.host, target.port, *astuple(tally))
                for target, tally in self._state.destinations.items()
            )


def is_refused_address(address: IPAddress) -> bool:
    """Addresses that reach this machine or its link instead of a network host.

    Private LAN ranges stay allowed.
    """

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return is_refused_address(address.ipv4_mapped)
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or (isinstance(address, ipaddress.IPv4Address) and address in _THIS_NETWORK)
    )


async def _open_upstream(target: wire.ProxyTarget) -> _Stream | _DialFailure:
    """Resolve once, then dial only addresses that passed the policy."""

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            target.host, target.port, type=socket.SOCK_STREAM
        )
    except (OSError, UnicodeError):
        return "unreachable"
    addresses = dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos)
    allowed = [address for address in addresses if not is_refused_address(address)]
    if not allowed:
        return "refused"
    for address in allowed:
        try:
            return await asyncio.open_connection(
                str(address), target.port, limit=HEAD_LIMIT_BYTES
            )
        except OSError:
            continue
    return "unreachable"


def _failure_reply(request: wire.ProxyRequest, failure: _DialFailure) -> bytes:
    if isinstance(request, wire.SocksConnect):
        code = (
            wire.SOCKS_NOT_ALLOWED
            if failure == "refused"
            else wire.SOCKS_HOST_UNREACHABLE
        )
        return wire.socks_reply(code)
    return wire.FORBIDDEN if failure == "refused" else wire.BAD_GATEWAY


def _token_key(token: str) -> bytes:
    # Leases are looked up by digest, so lookup time does not depend on how
    # much of a guessed token matches a real one.
    return hashlib.sha256(token.encode("utf-8")).digest()


class _EgressProxy:
    def __init__(self) -> None:
        self._start_lock = threading.Lock()
        self._running: tuple[asyncio.AbstractEventLoop, int] | None = None
        self._lock = threading.Lock()
        self._leases: dict[bytes, _LeaseState] = {}
        self._open_connections = 0  # proxy loop only

    def _ensure_started(self) -> tuple[asyncio.AbstractEventLoop, int]:
        with self._start_lock:
            if self._running is None:
                self._running = self._start()
            return self._running

    def _start(self) -> tuple[asyncio.AbstractEventLoop, int]:
        loop = asyncio.new_event_loop()
        bound: concurrent.futures.Future[int] = concurrent.futures.Future()

        def run() -> None:
            try:
                server = loop.run_until_complete(
                    asyncio.start_server(
                        self._serve, PROXY_HOST, 0, limit=HEAD_LIMIT_BYTES
                    )
                )
            except OSError as exc:
                bound.set_exception(exc)
                loop.close()
                return
            bound.set_result(server.sockets[0].getsockname()[1])
            loop.run_forever()

        try:
            threading.Thread(target=run, name="egress-proxy", daemon=True).start()
            return loop, bound.result(timeout=_START_TIMEOUT_S)
        except (RuntimeError, OSError) as exc:
            raise EgressProxyStartError("network proxy could not start") from exc

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[EgressLease]:
        loop, port = self._ensure_started()
        token = secrets.token_hex(_TOKEN_BYTES)
        key = _token_key(token)
        state = _LeaseState()
        with self._lock:
            self._leases[key] = state
        try:
            yield EgressLease(
                http_proxy_url=f"http://{token}:x@{PROXY_HOST}:{port}",
                socks_proxy_url=f"socks5h://{token}:x@{PROXY_HOST}:{port}",
                _state=state,
                _lock=self._lock,
            )
        finally:
            with self._lock:
                del self._leases[key]
            # Runs on the proxy loop, where connections are registered, so none
            # authorized before the token was removed can be missed.
            closed = asyncio.run_coroutine_threadsafe(_abort_connections(state), loop)
            # Shielded so a canceled caller still leaves no tunnel open.
            await asyncio.shield(asyncio.wrap_future(closed))

    def _authorize(
        self,
        token: str | None,
        task: asyncio.Task[object],
        writer: asyncio.StreamWriter,
    ) -> _LeaseState | None:
        if token is None:
            return None
        with self._lock:
            state = self._leases.get(_token_key(token))
        if state is not None:
            state.connections[task] = [writer]
        return state

    def _tally(self, state: _LeaseState, target: wire.ProxyTarget) -> _Tally:
        with self._lock:
            tally = state.destinations.setdefault(target, _Tally())
        tally.connections += 1
        return tally

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._open_connections >= MAX_OPEN_CONNECTIONS:
            writer.transport.abort()
            return
        self._open_connections += 1
        task = asyncio.current_task()
        assert task is not None  # start_server runs each connection as a task
        state: _LeaseState | None = None
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
                first_byte = await reader.readexactly(1)
                request: wire.ProxyRequest
                if first_byte[0] == wire.SOCKS_VERSION:
                    token = await wire.read_socks_token(reader, writer)
                    state = self._authorize(token, task, writer)
                    writer.write(wire.socks_auth_reply(accepted=state is not None))
                    if state is None:
                        return
                    request = wire.SocksConnect(await wire.read_socks_target(reader))
                else:
                    request = await wire.read_http_request(first_byte, reader)
                    state = self._authorize(request.token, task, writer)
                    if state is None:
                        writer.write(wire.PROXY_AUTH_REQUIRED)
                        return
            await self._connect(state, task, request, (reader, writer))
        except wire.HandshakeRejected as exc:
            writer.write(exc.reply)
        except (EOFError, OSError):
            pass  # The client left, reset, or stalled past the handshake timeout.
        finally:
            self._open_connections -= 1
            writer.close()
            if state is not None:
                del state.connections[task]

    async def _connect(
        self,
        state: _LeaseState,
        task: asyncio.Task[object],
        request: wire.ProxyRequest,
        client: _Stream,
    ) -> None:
        tally = self._tally(state, request.target)
        upstream = await _open_upstream(request.target)
        if isinstance(upstream, str):
            tally.failed += 1
            client[1].write(_failure_reply(request, upstream))
            return
        state.connections[task].append(upstream[1])
        try:
            if isinstance(request, wire.HttpForward):
                responded = await forward(
                    client,
                    upstream,
                    head=request.head,
                    body=request.body,
                    count_sent=tally.count_sent,
                    count_received=tally.count_received,
                )
                if not responded:
                    client[1].write(wire.BAD_GATEWAY)
                return
            client[1].write(
                wire.CONNECT_ESTABLISHED
                if isinstance(request, wire.HttpConnect)
                else wire.socks_reply(wire.SOCKS_SUCCEEDED)
            )
            await tunnel(
                client,
                upstream,
                count_sent=tally.count_sent,
                count_received=tally.count_received,
            )
        finally:
            upstream[1].close()


async def _abort_connections(state: _LeaseState) -> None:
    """End a lease's connections now, discarding anything still unsent.

    ``close()`` would keep a socket open until a peer that stopped reading drains
    its buffer, so data would keep flowing after the lease ended.
    """

    tasks = tuple(state.connections)
    for task, writers in state.connections.items():
        for writer in writers:
            writer.transport.abort()
        task.cancel()
    if tasks:
        await asyncio.wait(tasks)


_PROCESS_PROXY = _EgressProxy()


def command_egress_lease() -> AbstractAsyncContextManager[EgressLease]:
    """Lease the process's proxy for one command.

    Entering starts the proxy on first use and raises ``EgressProxyStartError``
    when it cannot. Exiting invalidates the lease's token and closes its open
    connections before returning, so ``destinations()`` is then final.
    """

    return _PROCESS_PROXY.lease()

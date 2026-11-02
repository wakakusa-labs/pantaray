"""Real-socket tests of the command egress proxy.

The proxy refuses loopback destinations, and every test server here listens on
loopback. Tests that need to reach one patch ``is_refused_address`` (the policy
function the proxy calls at dial time) to allow everything; the refusal tests
run against the real policy.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit

import pytest

from pantaray_agents.local_runtime.tooling.sandbox import egress_proxy
from pantaray_agents.local_runtime.tooling.sandbox.egress_proxy import (
    EgressLease,
    NetworkDestination,
    command_egress_lease,
    is_refused_address,
)

LOOPBACK = "127.0.0.1"
HEAD_END = b"\r\n\r\n"
IO_TIMEOUT_S = 5.0


@dataclass
class _Server:
    port: int
    connections: int = 0
    heads: list[bytes] = field(default_factory=list)


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_proxy, "is_refused_address", lambda _address: False)


type _Handler = Callable[
    [_Server, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


@asynccontextmanager
async def _listening(handler: _Handler) -> AsyncIterator[_Server]:
    state = _Server(port=0)
    server = await asyncio.start_server(partial(handler, state), LOOPBACK, 0)
    state.port = server.sockets[0].getsockname()[1]
    try:
        yield state
    finally:
        # No wait_closed(): a tunnel a failing test left open must not hang here.
        server.close()


async def _echo(
    state: _Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    state.connections += 1
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()
    writer.close()


def _http(name: str) -> _Handler:
    """A keep-alive HTTP/1.1 server that answers ``<name> <request-target>``."""

    async def serve(
        state: _Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        state.connections += 1
        while True:
            try:
                head = await reader.readuntil(HEAD_END)
            except asyncio.IncompleteReadError:
                break
            state.heads.append(head)
            body = f"{name} {head.split(b' ')[1].decode()}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
            writer.write(body)
            await writer.drain()
        writer.close()

    return serve


@pytest.fixture
async def echo_server() -> AsyncIterator[_Server]:
    async with _listening(_echo) as state:
        yield state


def _proxy(lease: EgressLease) -> tuple[str, int]:
    parts = urlsplit(lease.http_proxy_url)
    assert parts.username is not None and parts.port is not None
    return parts.username, parts.port


def _basic(token: str) -> bytes:
    return b"Proxy-Authorization: Basic " + base64.b64encode(f"{token}:x".encode())


async def _open(
    lease: EgressLease,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(LOOPBACK, _proxy(lease)[1])


async def _read_all(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.read(), IO_TIMEOUT_S)


def _connect_head(target: str, token: str | None) -> bytes:
    auth = _basic(token) + b"\r\n" if token else b""
    return f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode() + auth + b"\r\n"


def _socks_greeting(token: str) -> bytes:
    user = token.encode()
    return bytes([5, 1, 2, 1, len(user)]) + user + bytes([1]) + b"x"


async def test_connect_tunnel_relays_bytes_and_records_destination(
    allow_loopback: None, echo_server: _Server
) -> None:
    # Catches: bytes the client sent right behind the CONNECT head being lost,
    # half-close not passed through, and byte counts that miss a direction.
    payload = os.urandom(300_000)
    async with command_egress_lease() as lease:
        reader, writer = await _open(lease)
        writer.write(
            _connect_head(f"{LOOPBACK}:{echo_server.port}", _proxy(lease)[0]) + b"early"
        )
        assert (await reader.readuntil(HEAD_END)).startswith(b"HTTP/1.1 200")
        writer.write(payload)
        writer.write_eof()
        assert await _read_all(reader) == b"early" + payload
        writer.close()
    size = len(payload) + len(b"early")
    assert lease.destinations() == (
        NetworkDestination(LOOPBACK, echo_server.port, 1, 0, size, size),
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
async def test_curl_reaches_server_through_http_connect_and_socks5h(
    allow_loopback: None,
) -> None:
    # Catches: the proxy URLs not working as handed to real clients, including
    # SOCKS5 username/password auth and a name resolved by the proxy.
    async with _listening(_http("S")) as state, command_egress_lease() as lease:
        http_url, socks_url = lease.http_proxy_url, lease.socks_proxy_url
        outputs = []
        for args in (
            f"-p -x {http_url} http://{LOOPBACK}:{state.port}/tunnel",
            f"-x {socks_url} http://localhost:{state.port}/socks",
        ):
            curl = await asyncio.create_subprocess_exec(
                *f"curl -sS --max-time 10 {args}".split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.environ["PATH"]},
            )
            stdout, stderr = await curl.communicate()
            assert curl.returncode == 0, stderr
            outputs.append(stdout)
    assert outputs == [b"S /tunnel", b"S /socks"]
    assert [(d.host, d.connections, d.failed) for d in lease.destinations()] == [
        (LOOPBACK, 1, 0),
        ("localhost", 1, 0),
    ]


def _rejections(token: str, port: int) -> dict[str, tuple[bytes, bytes]]:
    target = f"{LOOPBACK}:{port}"
    connect_request = bytes([5, 1, 0, 1, 127, 0, 0, 1]) + port.to_bytes(2)
    return {
        "http unknown token": (_connect_head(target, "f" * 64), b"HTTP/1.1 407"),
        "http no credentials": (_connect_head(target, None), b"HTTP/1.1 407"),
        "socks unknown token": (_socks_greeting("f" * 64), bytes([5, 2, 1, 1])),
        "socks no-auth only": (bytes([5, 1, 0]), bytes([5, 0xFF])),
        "socks bind": (
            _socks_greeting(token) + bytes([5, 2]) + connect_request[2:],
            bytes([5, 2, 1, 0, 5, 7]),
        ),
        "socks udp associate": (
            _socks_greeting(token) + bytes([5, 3]) + connect_request[2:],
            bytes([5, 2, 1, 0, 5, 7]),
        ),
    }


@pytest.mark.parametrize("case", list(_rejections("t", 1)))
async def test_rejected_handshakes_never_connect(
    allow_loopback: None, echo_server: _Server, case: str
) -> None:
    # Catches: a guessed or missing token, a client that skips auth, or a
    # command other than CONNECT still reaching the destination.
    async with command_egress_lease() as lease:
        request, expected = _rejections(_proxy(lease)[0], echo_server.port)[case]
        reader, writer = await _open(lease)
        writer.write(request)
        assert (await _read_all(reader)).startswith(expected)
        writer.close()
    assert echo_server.connections == 0
    assert lease.destinations() == ()


async def test_non_public_destinations_are_refused_and_recorded(
    echo_server: _Server,
) -> None:
    # Catches: the real policy letting a name or mapped literal that resolves
    # to loopback or the metadata address through, or not recording a refusal.
    port = echo_server.port
    async with command_egress_lease() as lease:
        token = _proxy(lease)[0]
        replies = []
        for target in (
            f"{LOOPBACK}:{port}",
            f"[::ffff:127.0.0.1]:{port}",
            f"localhost:{port}",
        ):
            reader, writer = await _open(lease)
            writer.write(_connect_head(target, token))
            replies.append((await _read_all(reader)).split(b"\r\n")[0])
            writer.close()
        reader, writer = await _open(lease)
        writer.write(
            _socks_greeting(token) + bytes([5, 1, 0, 1, 169, 254, 169, 254, 0, 80])
        )
        replies.append(await _read_all(reader))
        writer.close()
    assert replies == [b"HTTP/1.1 403 Forbidden"] * 3 + [
        bytes([5, 2, 1, 0, 5, 2, 0, 1, 0, 0, 0, 0, 0, 0])
    ]
    assert echo_server.connections == 0
    assert [(d.port, d.connections, d.failed) for d in lease.destinations()] == [
        (port, 1, 1)
    ] * 3 + [(80, 1, 1)]
    assert lease.destinations()[2].host == "localhost"


_REFUSED = (
    "127.0.0.1 0.0.0.0 0.1.2.3 169.254.169.254 224.0.0.251 :: ::1 fe80::1 ff02::1"
)
_REFUSED_MAPPED = "::ffff:127.0.0.1 ::ffff:169.254.169.254 ::ffff:0.1.2.3"
_ALLOWED = "10.0.0.5 192.168.1.10 ::ffff:93.184.216.34 2606:4700::1111"


def test_refused_address_policy() -> None:
    # Catches: IPv4-mapped IPv6 slipping past the IPv4 checks, and private LAN
    # addresses being blocked, which the current command behavior allows.
    def refused(addresses: str) -> list[bool]:
        return [is_refused_address(ipaddress.ip_address(a)) for a in addresses.split()]

    assert all(refused(_REFUSED + " " + _REFUSED_MAPPED))
    assert not any(refused(_ALLOWED))


async def test_lease_end_closes_tunnels_and_invalidates_token(
    allow_loopback: None, echo_server: _Server
) -> None:
    # Catches: a command's tunnel outliving its lease, and an ended lease's
    # token still opening connections.
    target = f"{LOOPBACK}:{echo_server.port}"
    async with command_egress_lease() as lease:
        token = _proxy(lease)[0]
        reader, writer = await _open(lease)
        writer.write(_connect_head(target, token) + b"ping")
        await reader.readuntil(HEAD_END)
        assert await reader.readexactly(4) == b"ping"
    assert await _read_all(reader) == b""
    writer.close()
    reader, writer = await _open(lease)
    writer.write(_connect_head(target, token))
    assert (await _read_all(reader)).startswith(b"HTTP/1.1 407")
    writer.close()
    assert echo_server.connections == 1


async def test_handshake_bounds_end_stalled_or_huge_requests_not_quiet_tunnels(
    allow_loopback: None, echo_server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: a client that never finishes its request holding a connection
    # forever, unbounded buffering of a head that never ends, and the handshake
    # timeout also cutting an established tunnel.
    monkeypatch.setattr(egress_proxy, "HANDSHAKE_TIMEOUT_S", 0.3)
    async with command_egress_lease() as lease:
        huge_reader, huge_writer = await _open(lease)
        huge_writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nX: " + b"a" * 70_000)
        assert (await _read_all(huge_reader)).startswith(b"HTTP/1.1 431")
        huge_writer.close()
        stalled_reader, stalled_writer = await _open(lease)
        stalled_writer.write(b"CONNECT 127.0.0.1")
        reader, writer = await _open(lease)
        writer.write(_connect_head(f"{LOOPBACK}:{echo_server.port}", _proxy(lease)[0]))
        await reader.readuntil(HEAD_END)
        assert await _read_all(stalled_reader) == b""
        await asyncio.sleep(0.6)
        writer.write(b"still open")
        assert await reader.readexactly(10) == b"still open"
        stalled_writer.close()
        writer.close()

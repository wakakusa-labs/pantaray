"""Real-socket tests of the command egress proxy.

The proxy refuses loopback destinations, and every test server here listens on
loopback. Tests that need to reach one patch ``is_refused_address`` (the policy
function the proxy calls at dial time) to allow everything; the refusal tests
run against the real policy.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import ipaddress
import os
import shutil
import signal
import time
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


def _http(name: str, *, honor_close: bool = True) -> _Handler:
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
            if honor_close and b"\r\nconnection: close" in head.lower():
                break
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


async def test_forward_requests_to_two_hosts_over_one_client_connection(
    allow_loopback: None,
) -> None:
    # Catches: a reused client connection carrying the second request to the
    # first host or recording it there, a response that does not tell the
    # client to reconnect, and proxy credentials leaking to the server.
    async with (
        _listening(_http("A")) as a,
        _listening(_http("B")) as b,
        command_egress_lease() as lease,
    ):
        token, port = _proxy(lease)
        auth = "Basic " + base64.b64encode(f"{token}:x".encode()).decode()

        def fetch_both() -> tuple[bytes, bytes]:
            connection = http.client.HTTPConnection(LOOPBACK, port, timeout=5)
            headers = {"Proxy-Authorization": auth, "Proxy-Connection": "keep-alive"}
            connection.request(
                "GET", f"http://{LOOPBACK}:{a.port}/one", headers=headers
            )
            first = connection.getresponse().read()
            # "localhost" also resolves to ::1, where nothing listens here.
            connection.request(
                "GET", f"http://localhost:{b.port}/two?q=1", headers=headers
            )
            second = connection.getresponse().read()
            connection.close()
            return first, second

        assert await asyncio.to_thread(fetch_both) == (b"A /one", b"B /two?q=1")
    assert (a.connections, len(a.heads), b.connections, len(b.heads)) == (1, 1, 1, 1)
    for head in (*a.heads, *b.heads):
        assert b"proxy-" not in head.lower()
        assert head.lower().count(b"connection:") == 1
        assert b"\r\nConnection: close\r\n" in head
    assert b.heads[0].startswith(b"GET /two?q=1 HTTP/1.1\r\nHost: localhost:")
    assert [
        (d.host, d.port, d.connections, d.failed) for d in lease.destinations()
    ] == [
        (LOOPBACK, a.port, 1, 0),
        ("localhost", b.port, 1, 0),
    ]


async def test_pipelined_second_request_never_reaches_the_first_host(
    allow_loopback: None,
) -> None:
    # Catches: relaying the rest of the client stream after the first request,
    # which would deliver a request meant for host B to host A when A ignores
    # Connection: close, and the client's close not reaching such a server.
    async with (
        _listening(_http("A", honor_close=False)) as a,
        _listening(_http("B")) as b,
        command_egress_lease() as lease,
    ):
        token = _proxy(lease)[0]
        reader, writer = await _open(lease)
        writer.write(
            b"".join(
                f"GET http://{LOOPBACK}:{server.port}/x HTTP/1.1\r\n".encode()
                + _basic(token)
                + HEAD_END
                for server in (a, b)
            )
        )
        writer.write_eof()
        response = await _read_all(reader)
        writer.close()
    assert response.endswith(b"\r\n\r\nA /x")
    assert response.count(b"HTTP/1.1 200") == 1
    assert (len(a.heads), b.connections) == (1, 0)
    assert [d.port for d in lease.destinations()] == [a.port]


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
            f"-x {http_url} http://{LOOPBACK}:{state.port}/forward",
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
    assert outputs == [b"S /forward", b"S /tunnel", b"S /socks"]
    assert [(d.host, d.connections, d.failed) for d in lease.destinations()] == [
        (LOOPBACK, 2, 0),
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
        huge_writer.write(b"GET http://example.com/ HTTP/1.1\r\nX: " + b"a" * 70_000)
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


type _Stream = tuple[asyncio.StreamReader, asyncio.StreamWriter]


async def _ended(reader: asyncio.StreamReader) -> bool:
    try:
        return await _read_all(reader) == b""
    except ConnectionError:
        return True


async def _tunnel(lease: EgressLease, target: str) -> _Stream | None:
    """A CONNECT tunnel, or None when the proxy closed the connection."""

    reader, writer = await _open(lease)
    writer.write(_connect_head(target, _proxy(lease)[0]))
    try:
        head = await asyncio.wait_for(reader.readuntil(HEAD_END), IO_TIMEOUT_S)
    except (asyncio.IncompleteReadError, ConnectionError):
        writer.close()
        return None
    assert head.startswith(b"HTTP/1.1 200")
    return reader, writer


async def _tunnel_once_free(lease: EgressLease, target: str) -> _Stream:
    async with asyncio.timeout(IO_TIMEOUT_S):
        while (stream := await _tunnel(lease, target)) is None:
            await asyncio.sleep(0.05)
    return stream


async def _echoes(stream: _Stream) -> bool:
    stream[1].write(b"ping")
    return await asyncio.wait_for(stream[0].readexactly(4), IO_TIMEOUT_S) == b"ping"


async def test_lease_end_discards_unsent_bytes_when_the_peer_stops_reading(
    allow_loopback: None,
) -> None:
    # Catches: closing rather than aborting at lease end. close() keeps both
    # sockets open while a peer that stopped reading holds the send buffer, so
    # once it reads again the proxy delivers bytes after the lease ended that
    # its record never counted.
    reading = asyncio.Event()
    delivered: list[int] = []

    async def sink(
        state: _Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        state.connections += 1
        await reading.wait()
        total = 0
        try:
            while chunk := await reader.read(65536):
                total += len(chunk)
        except ConnectionError:
            pass
        delivered.append(total)
        writer.close()

    async def flood(writer: asyncio.StreamWriter) -> None:
        while True:
            writer.write(b"x" * 65536)
            await writer.drain()

    async with _listening(sink) as server:
        async with command_egress_lease() as lease:
            reader, writer = await _tunnel_once_free(lease, f"{LOOPBACK}:{server.port}")
            flooding = asyncio.create_task(flood(writer))
            sent = -1
            while sent != (sent := lease.destinations()[0].bytes_sent):
                await asyncio.sleep(0.2)  # until every buffer on the way is full
        reading.set()
        async with asyncio.timeout(IO_TIMEOUT_S):
            while not delivered:
                await asyncio.sleep(0.05)
        assert await _ended(reader)
        flooding.cancel()
        await asyncio.wait([flooding])
        writer.close()
    assert 0 < delivered[0] <= lease.destinations()[0].bytes_sent


async def test_connection_cap_closes_extra_connections_and_frees_slots(
    allow_loopback: None, echo_server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: idle unauthenticated connections piling up until the backend
    # runs out of descriptors, a cap that cuts connections already open, and
    # slots that never come back after a timeout or a close.
    monkeypatch.setattr(egress_proxy, "MAX_OPEN_CONNECTIONS", 2)
    monkeypatch.setattr(egress_proxy, "HANDSHAKE_TIMEOUT_S", 0.5)
    target = f"{LOOPBACK}:{echo_server.port}"
    async with command_egress_lease() as idle_lease, command_egress_lease() as lease:
        idle_reader, idle_writer = await _open(idle_lease)
        first = await _tunnel(lease, target)
        assert first is not None
        assert await _tunnel(lease, target) is None
        assert await _echoes(first)
        assert await _ended(idle_reader)
        idle_writer.close()
        second = await _tunnel_once_free(lease, target)
        first[1].close()
        third = await _tunnel_once_free(lease, target)
        assert await _echoes(second) and await _echoes(third)
        second[1].close()
        third[1].close()


# The fork under test is from a process running the proxy thread, as the
# backend's own forks are.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
async def test_a_forked_child_leases_a_proxy_of_its_own() -> None:
    # Catches: a child forked after the proxy started inheriting its loop but
    # not the thread running it, so leaving its first lease waits forever.
    async with command_egress_lease() as parent_lease:
        parent_port = _proxy(parent_lease)[1]

    async def lease_in_child() -> int:
        async with command_egress_lease() as lease:
            return _proxy(lease)[1]

    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child reports through its status
        signal.alarm(10)
        try:
            port = asyncio.run(asyncio.wait_for(lease_in_child(), IO_TIMEOUT_S))
            os._exit(0 if port != parent_port else 2)
        except BaseException:
            os._exit(1)
    deadline = time.monotonic() + 15
    while (waited := os.waitpid(pid, os.WNOHANG))[0] == 0:
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the forked child hung leaving its lease")
        await asyncio.sleep(0.05)
    assert os.waitstatus_to_exitcode(waited[1]) == 0

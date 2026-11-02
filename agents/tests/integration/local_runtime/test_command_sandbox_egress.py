"""A networked command under the real seatbelt profile reaches hosts only
through its egress lease, and its result records where it went.

The proxy runs in this test process and refuses loopback destinations; tests
whose servers listen on loopback patch ``is_refused_address`` (the proxy's
dial-time policy) to let them through, as the proxy's own tests do.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import BrokerToolOutcome
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.local_runtime.tooling.sandbox import egress_proxy
from pantaray_agents.local_runtime.tooling.sandbox.sandbox_denial import (
    NETWORK_DENIAL_HINT,
)

from .support import (
    SEATBELT_SKIP_REASON,
    RuntimeIntegrationTestbed,
    bootstrap_runtime_testbed,
    compile_workspace_binary,
    execute_bash,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)

CONNECT_SOURCE = r"""
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>
int main(int argc, char **argv) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address = {0};
    address.sin_family = AF_INET;
    address.sin_port = htons(atoi(argv[2]));
    inet_pton(AF_INET, argv[1], &address.sin_addr);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        perror("connect"); return 111;
    }
    return 0;
}
"""
# A documentation address (RFC 5737): unsandboxed, it would only time out.
OUTSIDE_ADDRESS = "192.0.2.1"


@pytest.fixture
def networked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> RuntimeIntegrationTestbed:
    testbed = bootstrap_runtime_testbed(tmp_path=tmp_path, monkeypatch=monkeypatch)
    update_command_network_enabled(
        db_path=testbed.db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        command_network_enabled=True,
        now="2026-11-02T00:00:00Z",
    )
    return testbed


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_proxy, "is_refused_address", lambda _address: False)


class _Hello(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"hello from the server"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _http_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def _ssh_banner_server() -> Iterator[int]:
    """Answers one connection with an SSH banner, then hangs up."""

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with closing(connection):
            connection.sendall(b"SSH-2.0-pantaray-test\r\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        thread.join(timeout=5)


def _network(outcome: BrokerToolOutcome) -> list[dict[str, object]]:
    assert isinstance(outcome.output, dict)
    network = outcome.output["network"]
    assert isinstance(network, list)
    return network  # type: ignore[return-value]


@pytest.mark.usefixtures("allow_loopback")
@pytest.mark.asyncio
async def test_curl_reaches_a_server_through_the_proxy_and_it_is_recorded(
    networked: RuntimeIntegrationTestbed,
) -> None:
    with _http_server() as port:
        # --noproxy '' clears NO_PROXY, which keeps loopback direct.
        outcome = await execute_bash(
            testbed=networked,
            command=f"curl -sS --noproxy '' http://127.0.0.1:{port}/",
        )

    assert outcome.status == "success", outcome.output
    assert isinstance(outcome.output, dict)
    assert outcome.output["stdout"] == "hello from the server"
    [reached] = _network(outcome)
    assert reached["host"] == "127.0.0.1" and reached["port"] == port
    assert (reached["connections"], reached["failed"]) == (1, 0)
    assert isinstance(reached["bytes_received"], int) and reached["bytes_received"] > 0


@pytest.mark.asyncio
async def test_loopback_stays_reachable_directly_and_unrecorded(
    networked: RuntimeIntegrationTestbed,
) -> None:
    with _http_server() as port:
        outcome = await execute_bash(
            testbed=networked, command=f"curl -sS http://127.0.0.1:{port}/"
        )

    assert outcome.status == "success", outcome.output
    assert _network(outcome) == []


@pytest.mark.asyncio
async def test_a_direct_connection_to_another_host_is_refused(
    networked: RuntimeIntegrationTestbed,
) -> None:
    compile_workspace_binary(
        workspace_path=networked.context.workspace_path,
        executable_name="connect-probe",
        source_code=CONNECT_SOURCE,
    )

    outcome = await execute_bash(
        testbed=networked, command=f"connect-probe {OUTSIDE_ADDRESS} 80"
    )

    assert outcome.status == "error"
    assert isinstance(outcome.output, dict)
    assert outcome.output["exit_code"] == 111
    assert "Operation not permitted" in str(outcome.output["stderr"])
    assert _network(outcome) == []


@pytest.mark.asyncio
async def test_a_name_lookup_in_the_command_fails_and_points_to_the_proxy(
    networked: RuntimeIntegrationTestbed,
) -> None:
    outcome = await execute_bash(
        testbed=networked,
        command="curl -sS --noproxy '*' --max-time 10 https://example.com/",
    )

    assert outcome.status == "error"
    assert isinstance(outcome.output, dict)
    assert "Could not resolve host" in str(outcome.output["stderr"])
    error = outcome.output["error"]
    assert isinstance(error, dict) and error["llm_feedback"] == NETWORK_DENIAL_HINT


@pytest.mark.parametrize("route", ["direct", "proxy"])
@pytest.mark.asyncio
async def test_the_backend_port_stays_denied(
    networked: RuntimeIntegrationTestbed,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    # The proxy's real policy refuses loopback, where the backend listens.
    with _http_server() as port:
        monkeypatch.setenv("PANTARAY_LOCAL_BACKEND_BOUND_HOST", "127.0.0.1")
        monkeypatch.setenv("PANTARAY_LOCAL_BACKEND_BOUND_PORT", str(port))
        noproxy = "--noproxy ''" if route == "proxy" else ""
        outcome = await execute_bash(
            testbed=networked,
            command=f"curl -fsS -o /dev/null {noproxy} http://127.0.0.1:{port}/",
        )

    assert outcome.status == "error", outcome.output
    if route == "proxy":
        [refused] = _network(outcome)
        assert (refused["port"], refused["connections"], refused["failed"]) == (
            port,
            1,
            1,
        )


@pytest.mark.usefixtures("allow_loopback")
@pytest.mark.asyncio
async def test_git_over_ssh_goes_through_the_proxy_with_its_token(
    networked: RuntimeIntegrationTestbed,
) -> None:
    with _ssh_banner_server() as port:
        outcome = await execute_bash(
            testbed=networked,
            command=f"git ls-remote ssh://git@127.0.0.1:{port}/repo.git",
        )

    # The banner server hangs up, so ssh fails after the tunnel was made.
    assert outcome.status == "error"
    [reached] = _network(outcome)
    assert (reached["host"], reached["port"]) == ("127.0.0.1", port)
    assert (reached["connections"], reached["failed"]) == (1, 0)
    assert reached["bytes_received"] == len(b"SSH-2.0-pantaray-test\r\n")

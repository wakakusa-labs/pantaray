"""How an Action command with network on is given, and confined to, its lease.

Real sandbox-exec runs are in tests/integration/local_runtime; these cover what
CI can check: the profile, the environment, the ssh helper against a real proxy,
and the lease lifecycle around a command.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from tests.unit.local_runtime.test_seatbelt_profiles import _validated_command_request

from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    UnprojectedBrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.sandbox import command_egress, egress_proxy
from pantaray_agents.local_runtime.tooling.sandbox.command_egress import (
    COMMAND_NETWORK_UNAVAILABLE,
    run_command_via_sandbox,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    attach_canceled_command_output,
    canceled_command_output,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    SSH_PROXY_COMMAND_NAME,
    build_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.egress_proxy import (
    EgressLease,
    EgressProxyStartError,
    NetworkDestination,
    command_egress_lease,
)
from pantaray_agents.local_runtime.tooling.sandbox.seatbelt_profiles import (
    render_seatbelt_profile,
)
from pantaray_agents.tools.contract import BrokerPolicyError

HELPER_SOURCE = (
    Path(egress_proxy.__file__).with_name(SSH_PROXY_COMMAND_NAME).read_text()
)


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_proxy, "is_refused_address", lambda _address: False)


@asynccontextmanager
async def _echo_server() -> AsyncIterator[int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


def test_network_on_profile_reaches_loopback_alone() -> None:
    request = build_sandbox_request(
        request=_validated_command_request(network_policy="allow"),
        temp_dir=Path("/system-temp/worker-temp"),
        egress=None,
    )

    profile = render_seatbelt_profile(request)

    # No resolver socket, no network services, no outbound rule past loopback.
    assert "mDNSResponder" not in profile
    assert "(system-network)" not in profile
    assert '(remote tcp "*:*")' not in profile
    outbound = [
        line for line in profile.splitlines() if "(allow network-outbound" in line
    ]
    assert outbound == ['(allow network-outbound (remote ip "localhost:*"))']
    backend = '(deny network-outbound (remote tcp "localhost:49152"))'
    assert profile.index(outbound[0]) < profile.index(backend)


async def test_proxy_env_names_the_lease_and_survives_paths_with_spaces(
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / "Application Support" / "100% temp"
    temp_dir.mkdir(parents=True)
    validated = _validated_command_request(network_policy="allow").model_copy(
        update={"app_runtime_python": "/Apps/Pan taray.app/python3"}
    )

    async with command_egress_lease() as lease:
        env = build_sandbox_request(
            request=validated, temp_dir=temp_dir, egress=lease
        ).env

    proxy = lease.http_proxy_url
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert env[name] == proxy
    # http://: httpx fails at client creation on a socks5h:// ALL_PROXY.
    assert env["ALL_PROXY"] == env["all_proxy"] == proxy
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert env["PATH"] == "/workspace/.venv/bin"
    # git runs GIT_SSH_COMMAND with sh, and ssh runs ProxyCommand with sh after
    # expanding % tokens: both layers must give back the real paths.
    ssh_argv = shlex.split(env["GIT_SSH_COMMAND"])
    assert ssh_argv[:5] == ["ssh", "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    proxy_command = ssh_argv[6].removeprefix("ProxyCommand=")
    helper = temp_dir / SSH_PROXY_COMMAND_NAME
    assert shlex.split(proxy_command.replace("%%", "%").replace("'%h'", "HOST")) == [
        "/Apps/Pan taray.app/python3",
        "-I",
        str(helper),
        "HOST",
        "%p",
    ]
    assert helper.read_text() == HELPER_SOURCE
    # The token travels in the environment only, never in an argument.
    token = urlsplit(proxy).username
    assert token is not None and token not in env["GIT_SSH_COMMAND"]


def _run_helper(lease: EgressLease, *args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", "-c", HELPER_SOURCE, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"HTTPS_PROXY": lease.http_proxy_url},
    )


@pytest.mark.usefixtures("allow_loopback")
async def test_ssh_helper_tunnels_through_the_lease_and_is_recorded() -> None:
    async with _echo_server() as port, command_egress_lease() as lease:
        helper = _run_helper(lease, "127.0.0.1", str(port))
        stdout, stderr = await asyncio.to_thread(helper.communicate, b"ssh banner\n")

    assert (helper.returncode, stdout, stderr) == (0, b"ssh banner\n", b"")
    assert lease.destinations() == (
        NetworkDestination("127.0.0.1", port, 1, 0, 11, 11),
    )


async def test_ssh_helper_reports_a_refused_target_and_relays_nothing() -> None:
    # The real policy: the proxy refuses loopback.
    async with _echo_server() as port, command_egress_lease() as lease:
        helper = _run_helper(lease, "127.0.0.1", str(port))
        stdout, stderr = await asyncio.to_thread(helper.communicate, b"data")

    assert helper.returncode == 1
    assert stdout == b""
    assert stderr.startswith(f"sandbox proxy: 127.0.0.1:{port}: 403".encode())
    assert lease.destinations() == (NetworkDestination("127.0.0.1", port, 1, 1, 0, 0),)


def test_ssh_helper_refuses_a_host_that_could_end_the_request_line() -> None:
    helper = subprocess.run(
        [sys.executable, "-I", "-c", HELPER_SOURCE, "a\r\nX-Evil: 1", "22"],
        capture_output=True,
        env={"HTTPS_PROXY": "http://token:x@127.0.0.1:9"},
        check=False,
    )

    assert helper.returncode == 1
    assert helper.stderr == b"sandbox proxy: expected HOST PORT\n"


def _outcome() -> UnprojectedBrokerToolOutcome:
    return UnprojectedBrokerToolOutcome(
        status="success",
        output={"status": "success", "exit_code": 0, "stdout": "", "stderr": ""},
    )


async def _call(request_update: dict[str, Any]) -> UnprojectedBrokerToolOutcome:
    return await run_command_via_sandbox(
        context=object(),  # type: ignore[arg-type]
        request=_validated_command_request(network_policy="allow").model_copy(
            update=request_update
        ),
    )


@pytest.mark.usefixtures("allow_loopback")
async def test_a_networked_command_reports_where_it_went_once_its_lease_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def command(*, egress: EgressLease | None, **_: object) -> Any:
        assert egress is not None
        helper = _run_helper(egress, "127.0.0.1", str(port))
        await asyncio.to_thread(helper.communicate, b"hello")
        return _outcome()

    monkeypatch.setattr(command_egress, "run_command_in_sandbox", command)

    async with _echo_server() as port:
        outcome = await _call({})

    assert isinstance(outcome.output, dict)
    assert outcome.output["network"] == [
        {
            "host": "127.0.0.1",
            "port": port,
            "connections": 1,
            "failed": 0,
            "bytes_sent": 5,
            "bytes_received": 5,
        }
    ]


@pytest.mark.parametrize(
    "update", [{"network_policy": "deny"}, {"run_outside_sandbox": True}]
)
async def test_no_lease_without_network_or_outside_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, update: dict[str, Any]
) -> None:
    leases: list[EgressLease | None] = []

    async def command(*, egress: EgressLease | None, **_: object) -> Any:
        leases.append(egress)
        return _outcome()

    def no_lease() -> None:
        raise AssertionError("no lease expected")

    monkeypatch.setattr(command_egress, "run_command_in_sandbox", command)
    monkeypatch.setattr(command_egress, "command_egress_lease", no_lease)

    await _call(update)

    assert leases == [None]


async def test_a_proxy_that_cannot_start_refuses_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audits: list[dict[str, object]] = []

    @asynccontextmanager
    async def failing_lease() -> AsyncIterator[EgressLease]:
        raise EgressProxyStartError("network proxy could not start")
        yield  # pragma: no cover

    async def command(**_: object) -> Any:
        raise AssertionError("the command must not run without its proxy")

    monkeypatch.setattr(command_egress, "command_egress_lease", failing_lease)
    monkeypatch.setattr(command_egress, "run_command_in_sandbox", command)
    monkeypatch.setattr(
        command_egress, "write_command_invocation_audit", lambda **kw: audits.append(kw)
    )

    with pytest.raises(BrokerPolicyError) as refused:
        await _call({})

    assert refused.value.code == COMMAND_NETWORK_UNAVAILABLE
    assert [audit["terminal_outcome"] for audit in audits] == ["broker_failed"]


async def test_a_canceled_command_keeps_where_it_went(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def command(*, egress: EgressLease | None, **_: object) -> Any:
        canceled = asyncio.CancelledError()
        attach_canceled_command_output(canceled, stdout="partial", stderr="")
        raise canceled

    @asynccontextmanager
    async def lease_with_history() -> AsyncIterator[Any]:
        class _Lease:
            http_proxy_url = "http://token:x@127.0.0.1:9"

            def destinations(self) -> tuple[NetworkDestination, ...]:
                return (NetworkDestination("example.com", 443, 1, 0, 10, 20),)

        yield _Lease()

    monkeypatch.setattr(command_egress, "run_command_in_sandbox", command)
    monkeypatch.setattr(command_egress, "command_egress_lease", lease_with_history)

    with pytest.raises(asyncio.CancelledError) as canceled:
        await _call({})

    partial = canceled_command_output(canceled.value)
    assert partial is not None and partial.stdout == "partial"
    assert partial.network == (
        {
            "host": "example.com",
            "port": 443,
            "connections": 1,
            "failed": 0,
            "bytes_sent": 10,
            "bytes_received": 20,
        },
    )

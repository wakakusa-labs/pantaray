"""Prepare the helper request and its invocation-local generated script."""

from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import uuid
from pathlib import Path

from ...runtime.runtime_env import (
    read_local_backend_bound_host,
    read_local_backend_bound_port,
)
from ..brokering.broker_protocol import ValidatedCommandRequest
from ..models import BrokerNetworkPolicy
from .command_sandbox_protocol import (
    ActionSandboxStorage,
    BrokerToSandboxCommandRequest,
)
from .egress_proxy import EgressLease
from .macos_runtime import (
    app_python_runtime_root,
    toolchain_read_roots,
    user_temp_dir,
)

GENERATED_PYTHON_SCRIPT_NAME = "generated_main.py"
SSH_PROXY_COMMAND_NAME = "ssh_proxy_command.py"
_SSH_PROXY_COMMAND_SOURCE = Path(__file__).with_name(SSH_PROXY_COMMAND_NAME)
# Loopback stays direct: local servers, which the seatbelt profile lets through.
_NO_PROXY = "localhost,127.0.0.1,::1"


def build_sandbox_request(
    *,
    request: ValidatedCommandRequest,
    temp_dir: Path,
    egress: EgressLease | None,
) -> BrokerToSandboxCommandRequest:
    """``egress`` is the command's network lease, None when it has no network."""

    # Tools keep scratch files here whatever TMPDIR says (xcrun's cache,
    # `mktemp /tmp/...`) and read them back. The private-storage denies still
    # win beneath these roots.
    system_temp_roots = [user_temp_dir(), "/tmp"]
    return compose_sandbox_request(
        temp_dir=temp_dir,
        real_read_roots=[*request.real_read_roots, *system_temp_roots],
        real_write_roots=[*request.real_write_roots, str(temp_dir), *system_temp_roots],
        private_storage_roots=request.private_storage_roots,
        action_storage=ActionSandboxStorage(
            workspace_root=request.action_workspace_root,
            published_results_root=request.published_results_root,
        ),
        app_runtime_python=Path(request.app_runtime_python),
        cwd=request.cwd,
        argv=_resolve_sandbox_argv(request=request, temp_dir=temp_dir),
        env=request.env
        if egress is None
        else {
            **request.env,
            **_egress_env(
                egress,
                app_runtime_python=Path(request.app_runtime_python),
                temp_dir=temp_dir,
            ),
        },
        timeout_ms=request.timeout_ms,
        stdout_max_bytes=request.stdout_max_bytes,
        stderr_max_bytes=request.stderr_max_bytes,
        temp_storage_limit_bytes=request.temp_storage_limit_bytes,
        network_policy=request.network_policy,
        use_login_environment=request.use_login_environment,
        run_outside_sandbox=request.run_outside_sandbox,
    )


def compose_sandbox_request(
    *,
    temp_dir: Path,
    real_read_roots: list[str],
    real_write_roots: list[str],
    private_storage_roots: list[str],
    action_storage: ActionSandboxStorage | None,
    app_runtime_python: Path,
    cwd: str,
    argv: list[str],
    env: dict[str, str],
    timeout_ms: int,
    stdout_max_bytes: int,
    stderr_max_bytes: int,
    temp_storage_limit_bytes: int,
    network_policy: BrokerNetworkPolicy,
    use_login_environment: bool,
    run_outside_sandbox: bool,
) -> BrokerToSandboxCommandRequest:
    """Add what every sandboxed command shares; the caller owns its write roots."""

    raw_host = read_local_backend_bound_host()
    host = ipaddress.ip_address("127.0.0.1" if raw_host == "localhost" else raw_host)
    port = read_local_backend_bound_port()
    # A wildcard listener is reachable on every interface; loopback also covers IPv6.
    address = (
        "*" if host.is_unspecified else "localhost" if host.is_loopback else str(host)
    )
    if isinstance(host, ipaddress.IPv6Address) and not (
        host.is_unspecified or host.is_loopback
    ):
        address = f"[{host}]"
    return BrokerToSandboxCommandRequest(
        request_id=str(uuid.uuid4()),
        real_read_roots=_dedupe_root_strings(*real_read_roots, str(temp_dir)),
        real_write_roots=_dedupe_root_strings(*real_write_roots),
        private_storage_roots=private_storage_roots,
        action_storage=action_storage,
        app_runtime_root=str(app_python_runtime_root(app_runtime_python)),
        cwd=cwd,
        argv=argv,
        runtime_read_roots=_dedupe_root_strings(*toolchain_read_roots()),
        env={
            **env,
            "TMPDIR": str(temp_dir),
            # Outside the sandbox a stand-in HOME only misleads apps: Chrome,
            # finding no login keychain there, asks the user to create one.
            "HOME": str(
                Path.home()
                if use_login_environment or run_outside_sandbox
                else temp_dir
            ),
            "RUSTUP_HOME": os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")),
        },
        timeout_ms=timeout_ms,
        stdout_max_bytes=stdout_max_bytes,
        stderr_max_bytes=stderr_max_bytes,
        temp_dir=str(temp_dir),
        temp_storage_limit_bytes=temp_storage_limit_bytes,
        network_policy=network_policy,
        use_login_environment=use_login_environment,
        run_outside_sandbox=run_outside_sandbox,
        protected_backend_address=f"{address}:{port}",
    )


def _egress_env(
    lease: EgressLease, *, app_runtime_python: Path, temp_dir: Path
) -> dict[str, str]:
    """The variables that send a command's connections through ``lease``.

    With command networking on, the seatbelt profile lets a command reach
    loopback alone, so it reaches anything else only through the proxy these
    name. The ssh ProxyCommand helper is copied into ``temp_dir``, which the
    command can read wherever the app is installed.
    """

    helper = temp_dir / SSH_PROXY_COMMAND_NAME
    shutil.copyfile(_SSH_PROXY_COMMAND_SOURCE, helper)
    proxy = lease.http_proxy_url
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        # http://, not socks5h://, though the port speaks both: httpx builds a
        # SOCKS transport for ALL_PROXY as a client is created, and fails
        # without the socksio package before sending anything.
        "ALL_PROXY": proxy,
        "all_proxy": proxy,
        "NO_PROXY": _NO_PROXY,
        "no_proxy": _NO_PROXY,
        "NODE_USE_ENV_PROXY": "1",
        "GIT_SSH_COMMAND": _git_ssh_command(app_runtime_python, helper),
    }


def _git_ssh_command(app_runtime_python: Path, helper: Path) -> str:
    # ssh expands its % tokens in ProxyCommand and runs it with /bin/sh, so the
    # paths are quoted for that shell and their % doubled; git runs the whole
    # value with sh too. The mux options keep a ControlMaster setting in the
    # user's ssh config from making ssh bind a socket the sandbox denies.
    proxy_command = " ".join(
        (
            shlex.quote(str(app_runtime_python)).replace("%", "%%"),
            "-I",
            shlex.quote(str(helper)).replace("%", "%%"),
            "'%h'",
            "%p",
        )
    )
    return shlex.join(
        (
            "ssh",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            f"ProxyCommand={proxy_command}",
        )
    )


def _dedupe_root_strings(*roots: str) -> list[str]:
    return list(dict.fromkeys(str(Path(root).resolve()) for root in roots))


def _resolve_sandbox_argv(
    *,
    request: ValidatedCommandRequest,
    temp_dir: Path,
) -> list[str]:
    if request.generated_python_code is None:
        return list(request.argv)
    script_path = temp_dir / GENERATED_PYTHON_SCRIPT_NAME
    script_path.write_text(request.generated_python_code, encoding="utf-8")
    return [request.argv[0], str(script_path), *request.argv[1:]]

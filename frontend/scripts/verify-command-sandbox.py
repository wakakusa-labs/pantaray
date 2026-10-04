"""Exercise the installed command worker using the packaged Python interpreter."""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import socket
import sys
import tempfile
from contextlib import closing, suppress
from pathlib import Path

from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    ActionSandboxStorage,
    BrokerToSandboxCommandRequest,
    SandboxCommandCompletion,
    SandboxOutputChunk,
    decode_message,
    encode_message,
)


def build_probe_request(
    *,
    workspace: Path,
    temporary: Path,
    private: Path,
    enabled: bool,
    ordinary_port: int,
    protected_port: int,
) -> BrokerToSandboxCommandRequest:
    """Build the worker request.

    agents/tests/unit/local_runtime/test_packaged_sandbox_probe.py checks it
    against the sandbox protocol in PR CI; the probe itself runs only at release.
    """
    private_file = private / "private.txt"
    code = f"""
import pathlib, socket
try:
    pathlib.Path({str(private_file)!r}).read_text()
except PermissionError:
    pass
else:
    raise AssertionError('private file was readable')
for port, allowed in (({ordinary_port}, {enabled}), ({protected_port}, False)):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
    except PermissionError:
        assert not allowed, 'ordinary network connection was denied'
    else:
        assert allowed, 'protected or disabled network connection was allowed'
pathlib.Path('result.txt').write_text('sandbox ok')
print('sandbox ok')
"""
    return BrokerToSandboxCommandRequest(
        request_id=f"packaged-probe-{enabled}",
        real_read_roots=[str(workspace), str(temporary)],
        real_write_roots=[str(workspace), str(temporary)],
        private_storage_roots=[str(private)],
        action_storage=ActionSandboxStorage(
            plan_path=str(workspace / "plan.md"),
            workspace_root=str(workspace),
            published_results_root=str(workspace / "results"),
        ),
        app_runtime_root=str(Path(sys.prefix).resolve()),
        cwd=str(workspace),
        argv=[
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            shlex.join([sys.executable, "-I", "-c", code]),
        ],
        runtime_read_roots=[],
        env={"PATH": "/usr/bin:/bin", "HOME": str(temporary), "TMPDIR": str(temporary)},
        timeout_ms=10_000,
        stdout_max_bytes=65_536,
        stderr_max_bytes=65_536,
        temp_dir=str(temporary),
        temp_storage_limit_bytes=1_048_576,
        network_policy="allow" if enabled else "deny",
        protected_backend_address=f"*:{protected_port}",
        use_login_environment=False,
        run_outside_sandbox=False,
    )


async def verify_command(
    root: Path, *, enabled: bool, ordinary_port: int, protected_port: int
) -> None:
    workspace = root / f"workspace-{enabled}"
    temporary = root / f"temp-{enabled}"
    workspace.mkdir()
    temporary.mkdir()
    request = build_probe_request(
        workspace=workspace,
        temporary=temporary,
        private=root / "private",
        enabled=enabled,
        ordinary_port=ordinary_port,
        protected_port=protected_port,
    )
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    completion: SandboxCommandCompletion | None = None
    chunks: list[str] = []
    try:
        assert worker.stdin is not None and worker.stdout is not None
        async with asyncio.timeout(20):
            worker.stdin.write(encode_message(request))
            await worker.stdin.drain()
            worker.stdin.close()
            async for line in worker.stdout:
                message = decode_message(line.decode())
                if isinstance(message, SandboxOutputChunk):
                    chunks.append(message.data)
                elif isinstance(message, SandboxCommandCompletion):
                    completion = message
                    break
    finally:
        # The worker preserves its tracked group until its owner reclaims it.
        with suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
        await worker.wait()
    assert worker.stderr is not None
    errors = (await worker.stderr.read()).decode()
    assert completion is not None, errors
    assert completion.outcome == "exited" and completion.exit_code == 0, (
        completion,
        chunks,
        errors,
    )
    assert (workspace / "result.txt").read_text() == "sandbox ok"


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pantaray-packaged-sandbox-") as directory:
        root = Path(directory).resolve()
        (root / "private").mkdir()
        (root / "private/private.txt").write_text("synthetic private value")
        with (
            closing(socket.socket()) as ordinary,
            closing(socket.socket()) as protected,
        ):
            ordinary.bind(("127.0.0.1", 0))
            ordinary.listen()
            protected.bind(("127.0.0.1", 0))
            protected.listen()
            for enabled in (True, False):
                await verify_command(
                    root,
                    enabled=enabled,
                    ordinary_port=ordinary.getsockname()[1],
                    protected_port=protected.getsockname()[1],
                )
    print(
        "packaged command sandbox verified: Python, private files, network on/off, backend protection"
    )


if __name__ == "__main__":
    asyncio.run(main())

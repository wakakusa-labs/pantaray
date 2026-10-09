from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.sandbox import command_sandbox_worker
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    BrokerToSandboxCommandRequest,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    compose_sandbox_request,
)
from pantaray_agents.local_runtime.tooling.sandbox.unrecorded_command import (
    run_unrecorded_sandbox_command,
)

WORKER_MODULE = command_sandbox_worker.__name__
# Runs the real helper, except that its temp-quota walk fails once the command
# has started, the way a file vanishing mid-walk used to fail it.
FAILING_HELPER = """
import asyncio
from pantaray_agents.local_runtime.tooling.sandbox import command_sandbox_worker as worker

def fail_once_started(root):
    if (root / "started").exists():
        raise FileNotFoundError("temp file vanished during the quota walk")
    return 0

worker._temp_dir_usage_bytes = fail_once_started
raise SystemExit(asyncio.run(worker.main()))
"""


async def _spawn_failing_helper(
    program: str, *args: str, **kwargs: object
) -> asyncio.subprocess.Process:
    assert args == ("-m", WORKER_MODULE)
    return await asyncio.create_subprocess_exec(program, "-c", FAILING_HELPER, **kwargs)


def _sleeping_command(temp_dir: Path) -> BrokerToSandboxCommandRequest:
    return compose_sandbox_request(
        temp_dir=temp_dir,
        real_read_roots=[],
        real_write_roots=[],
        private_storage_roots=[],
        action_storage=None,
        app_runtime_python=Path(sys.executable),
        cwd=str(temp_dir),
        argv=["/bin/sh", "-c", ': > "$TMPDIR/started"; exec sleep 60'],
        env={"PATH": "/usr/bin:/bin"},
        timeout_ms=60_000,
        stdout_max_bytes=65_536,
        stderr_max_bytes=65_536,
        temp_storage_limit_bytes=1_048_576,
        network_policy="deny",
        use_login_environment=False,
        # Skips seatbelt only; the helper and the broker exchange are real.
        run_outside_sandbox=True,
    )


async def test_broker_returns_when_the_helper_fails_while_the_command_runs(
    tmp_path: Path,
) -> None:
    # Far below the command's own 60 s: the broker must not wait on it at all.
    async with asyncio.timeout(15):
        terminal_outcome, output = await run_unrecorded_sandbox_command(
            db_path=tmp_path / "runtime.db",
            build_request=_sleeping_command,
            create_subprocess_exec=_spawn_failing_helper,
        )

    assert terminal_outcome == "broker_failed"
    assert output.error is not None
    assert output.error.type == "CommandSandboxError"
    assert "temp file vanished during the quota walk" in output.stderr


async def test_failed_helper_keeps_its_group_when_the_broker_is_gone(
    tmp_path: Path,
) -> None:
    # A broker that has exited leaves the helper's stderr without a reader.
    stderr_read, stderr_write = os.pipe()
    os.close(stderr_read)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        FAILING_HELPER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_write,
        preexec_fn=os.setsid,
    )
    os.close(stderr_write)
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        request = _sleeping_command(tmp_path)
        process.stdin.write(request.model_dump_json().encode("utf-8") + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        async with asyncio.timeout(15):
            await process.stdout.read()

        # The leader stays until its group is killed, or the command is orphaned.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=1)
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def test_temp_usage_skips_a_file_removed_during_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "kept").write_bytes(b"k" * 10)
    removed = tmp_path / "removed"
    removed.write_bytes(b"r" * 100)
    real_is_file = Path.is_file

    def is_file_then_removed(path: Path) -> bool:
        result = real_is_file(path)
        if path == removed:
            # The command deletes its temp file between the check and the stat.
            path.unlink()
        return result

    monkeypatch.setattr(Path, "is_file", is_file_then_removed)

    assert command_sandbox_worker._temp_dir_usage_bytes(tmp_path) == 10

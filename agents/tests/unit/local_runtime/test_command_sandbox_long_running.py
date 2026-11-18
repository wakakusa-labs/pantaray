"""The sandbox helper's long-running mode, driven over its real stdin/stdout.

The test plays the parent: it writes the request and stop lines, reads the
message stream, and kills the helper's group after the exit report.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    spawn_sandbox_helper,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    BrokerToSandboxLongRunningRequest,
    SandboxLifecycleEvent,
    SandboxLongRunningMessage,
    SandboxOutputChunk,
    SandboxProcessExited,
    SandboxProcessSpawnFailed,
    SandboxStopRequest,
    decode_long_running_message,
    encode_message,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_request import (
    compose_sandbox_request,
    to_long_running_request,
)

READY = "ready"
# The one-shot limits the long-running request drops: far below what the
# commands here run for and write.
ONE_SHOT_TIMEOUT_MS = 100
ONE_SHOT_OUTPUT_MAX_BYTES = 1024
# Reaches the parent-exit watch: the helper is spawned by a parent that exits
# once the command has started, without killing anything.
ORPHANING_PARENT = """
import asyncio, os, sys
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_client import (
    spawn_sandbox_helper,
)

async def main():
    helper = await spawn_sandbox_helper(asyncio.create_subprocess_exec)
    helper.stdin.write(sys.stdin.buffer.readline())
    await helper.stdin.drain()
    while b'"started"' not in await helper.stdout.readline():
        pass
    print(helper.pid, flush=True)
    os._exit(0)

asyncio.run(main())
"""


def _request(
    temp_dir: Path,
    script: str,
    *,
    stop_grace_ms: int = 10_000,
    temp_storage_limit_bytes: int = 1_048_576,
    argv: list[str] | None = None,
    real_read_roots: list[str] | None = None,
    real_write_roots: list[str] | None = None,
    run_outside_sandbox: bool = True,
) -> BrokerToSandboxLongRunningRequest:
    temp_dir.mkdir(exist_ok=True)
    one_shot = compose_sandbox_request(
        temp_dir=temp_dir,
        real_read_roots=real_read_roots or [],
        real_write_roots=real_write_roots or [],
        # Every real command has some; seatbelt rejects an empty deny list.
        private_storage_roots=[str(temp_dir.parent / "private")],
        action_storage=None,
        app_runtime_python=Path(sys.executable),
        cwd=str(temp_dir),
        argv=argv or ["/bin/sh", "-c", script],
        env={"PATH": "/usr/bin:/bin"},
        timeout_ms=ONE_SHOT_TIMEOUT_MS,
        stdout_max_bytes=ONE_SHOT_OUTPUT_MAX_BYTES,
        stderr_max_bytes=ONE_SHOT_OUTPUT_MAX_BYTES,
        temp_storage_limit_bytes=temp_storage_limit_bytes,
        network_policy="deny",
        use_login_environment=False,
        # Skips seatbelt only, except where a test turns it on.
        run_outside_sandbox=run_outside_sandbox,
    )
    return to_long_running_request(one_shot, stop_grace_ms=stop_grace_ms)


class _Parent:
    def __init__(self, helper: asyncio.subprocess.Process, request_id: str) -> None:
        self.helper = helper
        self.request_id = request_id
        self.stdout: list[str] = []
        self.stderr: list[str] = []

    async def next_message(self) -> SandboxLongRunningMessage:
        assert self.helper.stdout is not None
        raw_line = await self.helper.stdout.readline()
        assert raw_line, "the helper's output ended without a final message"
        message = decode_long_running_message(raw_line.decode("utf-8"))
        if isinstance(message, SandboxOutputChunk):
            (self.stdout if message.stream == "stdout" else self.stderr).append(
                message.data
            )
        return message

    async def wait_for_stdout(self, text: str) -> None:
        while text not in "".join(self.stdout):
            message = await self.next_message()
            assert not isinstance(
                message, SandboxProcessExited | SandboxProcessSpawnFailed
            ), message

    async def final_message(
        self,
    ) -> SandboxProcessExited | SandboxProcessSpawnFailed:
        while True:
            message = await self.next_message()
            if isinstance(message, SandboxProcessExited | SandboxProcessSpawnFailed):
                return message

    async def stop(self) -> None:
        assert self.helper.stdin is not None
        self.helper.stdin.write(
            encode_message(SandboxStopRequest(request_id=self.request_id))
        )
        await self.helper.stdin.drain()


@asynccontextmanager
async def _started(
    request: BrokerToSandboxLongRunningRequest,
) -> AsyncIterator[_Parent]:
    helper = await spawn_sandbox_helper(asyncio.create_subprocess_exec)
    try:
        assert helper.stdin is not None
        helper.stdin.write(encode_message(request))
        await helper.stdin.drain()
        async with asyncio.timeout(15):
            yield _Parent(helper, request.request_id)
    finally:
        # What the parent does after the exit report, or on any failure.
        with suppress(ProcessLookupError):
            os.killpg(helper.pid, signal.SIGKILL)
        await helper.wait()


async def test_a_command_runs_past_the_one_shot_timeout_and_reports_its_exit(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path / "temp", "sleep 0.5; echo done; exit 3")

    async with _started(request) as parent:
        started = await parent.next_message()
        final = await parent.final_message()

    assert isinstance(started, SandboxLifecycleEvent)
    assert started.event == "started"
    assert final == SandboxProcessExited(
        request_id=request.request_id, ended_by="command", exit_code=3, signal=None
    )
    assert "".join(parent.stdout) == "done\n"


async def test_a_chatty_command_is_streamed_whole_and_not_ended(
    tmp_path: Path,
) -> None:
    size = 4 * 1024 * 1024
    request = _request(
        tmp_path / "temp",
        f"head -c {size} /dev/zero | tr '\\0' a; printf tail >&2",
    )

    async with _started(request) as parent:
        final = await parent.final_message()

    assert isinstance(final, SandboxProcessExited)
    assert (final.ended_by, final.exit_code) == ("command", 0)
    assert "".join(parent.stdout) == "a" * size
    assert "".join(parent.stderr) == "tail"


async def test_stop_sends_sigterm_and_reports_how_the_command_handled_it(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path / "temp",
        f"trap 'exit 7' TERM; echo {READY}; while :; do sleep 0.05; done",
        stop_grace_ms=10_000,
    )

    async with _started(request) as parent:
        await parent.wait_for_stdout(READY)
        stopped_at = time.monotonic()
        await parent.stop()
        # A second stop changes nothing.
        await parent.stop()
        final = await parent.final_message()

    # Well inside the grace period: the command ended on SIGTERM.
    assert time.monotonic() - stopped_at < 5
    assert final == SandboxProcessExited(
        request_id=request.request_id, ended_by="stop", exit_code=7, signal=None
    )


async def test_stop_kills_a_command_that_ignores_sigterm_after_the_grace(
    tmp_path: Path,
) -> None:
    grace_ms = 300
    request = _request(
        tmp_path / "temp",
        f"trap '' TERM; echo {READY}; while :; do sleep 0.05; done",
        stop_grace_ms=grace_ms,
    )

    async with _started(request) as parent:
        await parent.wait_for_stdout(READY)
        stopped_at = time.monotonic()
        await parent.stop()
        final = await parent.final_message()

    assert time.monotonic() - stopped_at >= grace_ms / 1000
    assert final == SandboxProcessExited(
        request_id=request.request_id,
        ended_by="stop",
        exit_code=None,
        signal=signal.SIGKILL,
    )


async def test_the_temp_space_limit_still_ends_a_long_running_command(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path / "temp",
        'head -c 131072 /dev/zero > "$TMPDIR/spill"; exec sleep 30',
        temp_storage_limit_bytes=65_536,
    )

    async with _started(request) as parent:
        final = await parent.final_message()

    assert final == SandboxProcessExited(
        request_id=request.request_id,
        ended_by="temp_storage_limit",
        exit_code=None,
        signal=signal.SIGKILL,
    )


async def test_a_command_that_cannot_start_is_reported(tmp_path: Path) -> None:
    request = _request(
        tmp_path / "temp", "", argv=[str(tmp_path / "missing-executable")]
    )

    async with _started(request) as parent:
        final = await parent.final_message()

    assert isinstance(final, SandboxProcessSpawnFailed)
    assert "FileNotFoundError" in final.error


async def test_the_helper_kills_the_command_when_its_parent_dies(
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / "temp"
    pid_file = temp_dir / "command.pid"
    request = _request(temp_dir, f'echo $$ > "{pid_file}"; exec sleep 60')
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        ORPHANING_PARENT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    output, _ = await asyncio.wait_for(
        parent.communicate(encode_message(request)), timeout=15
    )
    helper_pid = int(output)
    try:
        async with asyncio.timeout(10):
            while not pid_file.exists() or not pid_file.read_text().strip():
                await asyncio.sleep(0.05)
            command_pid = int(pid_file.read_text())
            while True:
                try:
                    os.kill(command_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
    finally:
        with suppress(ProcessLookupError):
            os.killpg(helper_pid, signal.SIGKILL)


@pytest.mark.skipif(
    not Path("/usr/bin/sandbox-exec").is_file(), reason="needs macOS seatbelt"
)
async def test_a_long_running_command_runs_under_the_seatbelt_profile(
    tmp_path: Path,
) -> None:
    temp_dir = tmp_path / "temp"
    outside = tmp_path / "outside.txt"
    request = _request(
        temp_dir,
        f'echo in > "$TMPDIR/inside.txt" && echo out > "{outside}"',
        real_read_roots=[str(tmp_path)],
        real_write_roots=[str(temp_dir)],
        run_outside_sandbox=False,
    )

    async with _started(request) as parent:
        final = await parent.final_message()

    assert isinstance(final, SandboxProcessExited)
    assert final.exit_code not in (None, 0)
    assert (temp_dir / "inside.txt").read_text() == "in\n"
    assert not outside.exists()

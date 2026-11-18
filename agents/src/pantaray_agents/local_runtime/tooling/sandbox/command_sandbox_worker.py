from __future__ import annotations

import asyncio
import codecs
import os
import signal
import sys
import traceback
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pantaray_agents.utils.timestamps import format_iso8601_utc_z_milliseconds

from .command_sandbox_protocol import (
    BrokerToSandboxCommandRequest,
    BrokerToSandboxLongRunningRequest,
    SandboxCommandCompletion,
    SandboxCommandSpec,
    SandboxLifecycleEvent,
    SandboxOutputChunk,
    SandboxProcessExited,
    SandboxProcessSpawnFailed,
    SandboxStopRequest,
    decode_request,
    encode_message,
)
from .seatbelt_profiles import render_seatbelt_profile

SANDBOX_PROFILE_NAME = "command.sb"
OUTPUT_CHUNK_SIZE_BYTES = 4096
TEMP_QUOTA_POLL_INTERVAL_SECONDS = 0.25
BROKER_EXIT_POLL_INTERVAL_SECONDS = 0.5
# macOS has no subreaper: a process whose parent exits is reparented to launchd.
LAUNCHD_PID = 1


@dataclass
class StreamCapture:
    bytes_read: int = 0
    limited: bool = False


def _utc_now() -> str:
    # The sandbox worker stays off the runtime package; the event time is unread.
    return format_iso8601_utc_z_milliseconds(datetime.now(UTC))


def _emit_message(message: object) -> None:
    assert hasattr(message, "model_dump_json")
    sys.stdout.buffer.write(encode_message(message))  # type: ignore[arg-type]
    sys.stdout.buffer.flush()


def _temp_dir_usage_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        # The command creates and removes its temp files while this walk runs.
        with suppress(FileNotFoundError):
            if path.is_file():
                total += path.stat().st_size
    return total


async def _forward_stream(
    *,
    stream: asyncio.StreamReader,
    stream_name: Literal["stdout", "stderr"],
    request_id: str,
    # None for a long-running command, which no output volume ends.
    max_bytes: int | None,
    capture: StreamCapture,
    budget_exceeded: asyncio.Event,
) -> None:
    seq = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        data = await stream.read(OUTPUT_CHUNK_SIZE_BYTES)
        decoded = decoder.decode(data, final=not data)
        capture.bytes_read += len(data)
        _emit_message(
            SandboxOutputChunk(
                request_id=request_id,
                stream=stream_name,
                seq=seq,
                data=decoded,
            )
        )
        seq += 1
        if not data:
            return
        if max_bytes is not None and capture.bytes_read > max_bytes:
            capture.limited = True
            budget_exceeded.set()
            return


async def _monitor_temp_quota(
    *,
    temp_dir: Path,
    temp_storage_limit_bytes: int,
    budget_exceeded: asyncio.Event,
) -> bool:
    while True:
        if _temp_dir_usage_bytes(temp_dir) > temp_storage_limit_bytes:
            budget_exceeded.set()
            return True
        await asyncio.sleep(TEMP_QUOTA_POLL_INTERVAL_SECONDS)


def _budget_exceeded_kind(
    *,
    stdout_limited: bool,
    stderr_limited: bool,
    temp_quota_exceeded: bool,
) -> Literal["stdout_limit", "stderr_limit", "temp_storage_limit"] | None:
    if stdout_limited:
        return "stdout_limit"
    if stderr_limited:
        return "stderr_limit"
    if temp_quota_exceeded:
        return "temp_storage_limit"
    return None


def _profile_path(temp_dir: str) -> Path:
    return Path(temp_dir) / SANDBOX_PROFILE_NAME


def _launch_argv(request: SandboxCommandSpec) -> list[str]:
    if request.run_outside_sandbox:
        # The user approved this one command to run without the profile.
        return list(request.argv)
    profile_path = _profile_path(request.temp_dir)
    profile_path.write_text(render_seatbelt_profile(request), encoding="utf-8")
    return ["/usr/bin/sandbox-exec", "-f", str(profile_path), *request.argv]


def _emit_spawn_failed(request_id: str, error: OSError) -> None:
    stderr = f"{type(error).__name__}: {error}\n"
    _emit_message(
        SandboxOutputChunk(request_id=request_id, stream="stderr", seq=0, data=stderr)
    )
    _emit_message(
        SandboxCommandCompletion(
            request_id=request_id,
            outcome="spawn_failed",
            exit_code=None,
            signal=None,
            stdout_bytes=0,
            stderr_bytes=len(stderr.encode("utf-8")),
        )
    )


async def _run_helper(request: BrokerToSandboxCommandRequest) -> int:
    try:
        process = await asyncio.create_subprocess_exec(
            *_launch_argv(request),
            cwd=request.cwd,
            env=request.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Inherit the helper's tracked group so broker Stop/recovery owns both.
        )
    except OSError as error:
        _emit_spawn_failed(request.request_id, error)
        return 0
    _emit_message(
        SandboxLifecycleEvent(
            request_id=request.request_id,
            event="started",
            at=_utc_now(),
        )
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = StreamCapture()
    stderr = StreamCapture()
    budget_exceeded = asyncio.Event()
    stdout_task = asyncio.create_task(
        _forward_stream(
            stream=process.stdout,
            stream_name="stdout",
            request_id=request.request_id,
            max_bytes=request.stdout_max_bytes,
            capture=stdout,
            budget_exceeded=budget_exceeded,
        )
    )
    stderr_task = asyncio.create_task(
        _forward_stream(
            stream=process.stderr,
            stream_name="stderr",
            request_id=request.request_id,
            max_bytes=request.stderr_max_bytes,
            capture=stderr,
            budget_exceeded=budget_exceeded,
        )
    )
    temp_quota_task = asyncio.create_task(
        _monitor_temp_quota(
            temp_dir=Path(request.temp_dir),
            temp_storage_limit_bytes=request.temp_storage_limit_bytes,
            budget_exceeded=budget_exceeded,
        )
    )
    process_wait = asyncio.create_task(process.wait())
    budget_wait = asyncio.create_task(budget_exceeded.wait())
    finished = asyncio.gather(process_wait, stdout_task, stderr_task)
    timed_out = False
    temp_quota_exceeded = False
    try:
        async with asyncio.timeout(request.timeout_ms / 1000):
            await asyncio.wait(
                (finished, budget_wait, temp_quota_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if finished.done():
                await finished
            temp_quota_exceeded = temp_quota_task.done() and temp_quota_task.result()
    except TimeoutError:
        timed_out = True
    finally:
        tasks = (
            finished,
            process_wait,
            stdout_task,
            stderr_task,
            temp_quota_task,
            budget_wait,
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    stdout_bytes, stderr_bytes = stdout.bytes_read, stderr.bytes_read
    if timed_out or budget_exceeded.is_set():
        # The broker kills the tracked group after receiving this receipt. The
        # helper cannot kill its own group before the receipt reaches the broker.
        _emit_message(
            SandboxCommandCompletion(
                request_id=request.request_id,
                outcome="timed_out" if timed_out else "canceled",
                exit_code=None,
                signal=signal.SIGKILL,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                budget_exceeded_kind=None
                if timed_out
                else _budget_exceeded_kind(
                    stdout_limited=stdout.limited,
                    stderr_limited=stderr.limited,
                    temp_quota_exceeded=temp_quota_exceeded,
                ),
            )
        )
        return 0

    if process.returncode is None:
        raise RuntimeError("sandboxed command ended without a return code")
    if process.returncode < 0:
        _emit_message(
            SandboxCommandCompletion(
                request_id=request.request_id,
                outcome="signaled",
                exit_code=None,
                signal=abs(process.returncode),
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
            )
        )
        return 0
    _emit_message(
        SandboxCommandCompletion(
            request_id=request.request_id,
            outcome="exited",
            exit_code=process.returncode,
            signal=None,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
        )
    )
    return 0


def _kill_command(process: asyncio.subprocess.Process) -> None:
    # The command may end between the check and the kill.
    with suppress(ProcessLookupError):
        process.kill()


async def _read_stop_request(request_id: str) -> None:
    """Return once the parent asks to stop the command.

    End of input is not a stop: a parent that closed it still kills the group.
    """

    raw_line = await asyncio.to_thread(sys.stdin.buffer.readline)
    if not raw_line:
        await asyncio.Future()
    stop = SandboxStopRequest.model_validate_json(raw_line)
    if stop.request_id != request_id:
        raise ValueError(f"stop request for another command: {stop.request_id}")


async def _stop_command(
    *,
    process: asyncio.subprocess.Process,
    finished: asyncio.Future[tuple[int, None, None]],
    grace_seconds: float,
) -> None:
    # The command shares this helper's group, which the broker's group kill and
    # startup recovery reach. The helper ignores the SIGTERM it sends there, so
    # that it can still report how the command ended.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.killpg(os.getpgrp(), signal.SIGTERM)
    # Done once the command has exited and nothing else holds its output open.
    await asyncio.wait((finished,), timeout=grace_seconds)
    if not finished.done():
        # Whatever else in the group ignored SIGTERM dies with the group kill
        # the parent sends after the exit report.
        _kill_command(process)


async def _run_long_running(request: BrokerToSandboxLongRunningRequest) -> None:
    """Stream output until the command ends, the parent stops it, or temp is full."""

    try:
        process = await asyncio.create_subprocess_exec(
            *_launch_argv(request),
            cwd=request.cwd,
            env=request.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        _emit_message(
            SandboxProcessSpawnFailed(
                request_id=request.request_id, error=f"{type(error).__name__}: {error}"
            )
        )
        return
    _emit_message(
        SandboxLifecycleEvent(
            request_id=request.request_id, event="started", at=_utc_now()
        )
    )
    assert process.stdout is not None
    assert process.stderr is not None
    # Unwatched here: the quota task's own result ends the command, and no
    # output volume does.
    budget_exceeded = asyncio.Event()
    stdout_task = asyncio.create_task(
        _forward_stream(
            stream=process.stdout,
            stream_name="stdout",
            request_id=request.request_id,
            max_bytes=None,
            capture=StreamCapture(),
            budget_exceeded=budget_exceeded,
        )
    )
    stderr_task = asyncio.create_task(
        _forward_stream(
            stream=process.stderr,
            stream_name="stderr",
            request_id=request.request_id,
            max_bytes=None,
            capture=StreamCapture(),
            budget_exceeded=budget_exceeded,
        )
    )
    process_wait = asyncio.create_task(process.wait())
    finished = asyncio.gather(process_wait, stdout_task, stderr_task)
    temp_quota_task = asyncio.create_task(
        _monitor_temp_quota(
            temp_dir=Path(request.temp_dir),
            temp_storage_limit_bytes=request.temp_storage_limit_bytes,
            budget_exceeded=budget_exceeded,
        )
    )
    stop_task = asyncio.create_task(_read_stop_request(request.request_id))
    ended_by: Literal["command", "stop", "temp_storage_limit"] = "command"
    try:
        await asyncio.wait(
            (finished, temp_quota_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if finished.done():
            await finished
        elif temp_quota_task.done():
            temp_quota_task.result()
            ended_by = "temp_storage_limit"
            _kill_command(process)
        else:
            stop_task.result()
            ended_by = "stop"
            await _stop_command(
                process=process,
                finished=finished,
                grace_seconds=request.stop_grace_ms / 1000,
            )
        returncode = await process_wait
    finally:
        tasks = (
            finished,
            process_wait,
            stdout_task,
            stderr_task,
            temp_quota_task,
            stop_task,
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    _emit_message(
        SandboxProcessExited(
            request_id=request.request_id,
            ended_by=ended_by,
            exit_code=returncode if returncode >= 0 else None,
            signal=-returncode if returncode < 0 else None,
        )
    )


async def _kill_group_when_broker_exits() -> None:
    """End the command once the broker that owns this group is gone.

    The helper leads its own session, so a broker that exits without reaping it
    (app quit, crash, SIGKILL) would leave the group running until the next
    startup recovery - or for good, for a command no resource row records.
    """

    while os.getppid() != LAUNCHD_PID:
        await asyncio.sleep(BROKER_EXIT_POLL_INTERVAL_SECONDS)
    os.killpg(os.getpgrp(), signal.SIGKILL)


async def _serve_request() -> None:
    raw_line = await asyncio.to_thread(sys.stdin.buffer.readline)
    request = decode_request(raw_line.decode("utf-8"))
    try:
        if isinstance(request, BrokerToSandboxLongRunningRequest):
            await _run_long_running(request)
        else:
            await _run_helper(request)
    except Exception:
        # This process boundary reports every helper failure: the broker shows
        # this stderr when the output ends without a completion receipt. A gone
        # broker must not end the helper here, before it can kill the group.
        with suppress(BrokenPipeError):
            traceback.print_exc()
    # EOF tells the broker the helper is done, receipt or not. sys.stdout.close()
    # would leave fd 1 open (the interpreter opens it with closefd=False), so the
    # broker would wait forever. The tracked group leader then waits here for the
    # broker to reap it.
    os.close(sys.stdout.fileno())
    await asyncio.Future()


async def main() -> int:
    async with asyncio.TaskGroup() as group:
        group.create_task(_kill_group_when_broker_exits())
        await _serve_request()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

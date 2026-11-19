from __future__ import annotations

import asyncio
import codecs
import os
import signal
import sys
import traceback
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

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


def _emit_message(message: BaseModel) -> None:
    sys.stdout.buffer.write(encode_message(message))
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
    max_bytes: int,
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
        if capture.bytes_read > max_bytes:
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


class _ParentOutput(asyncio.Protocol):
    """The helper's stdout to the parent, written through the loop.

    A parent that stops reading pauses the command's pipes, not the loop: stop
    requests, the temp quota and the broker-exit watch keep running.
    """

    _transport: asyncio.WriteTransport

    def __init__(self) -> None:
        self._readers: list[asyncio.ReadTransport] = []
        self._paused = False
        self._closing = False
        # Done with an error once the parent closes the pipe without being told.
        self.lost: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    @classmethod
    async def open(cls) -> _ParentOutput:
        # Owns fd 1, so that closing this writer is the parent's EOF.
        pipe = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)
        _, output = await asyncio.get_running_loop().connect_write_pipe(cls, pipe)
        return output

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.WriteTransport)
        self._transport = transport

    def pause_writing(self) -> None:
        self._paused = True
        for reader in self._readers:
            reader.pause_reading()

    def resume_writing(self) -> None:
        self._paused = False
        for reader in self._readers:
            reader.resume_reading()

    def connection_lost(self, exc: Exception | None) -> None:
        if self.lost.done():
            return
        if self._closing:
            self.lost.set_result(None)
        else:
            self.lost.set_exception(BrokenPipeError("the parent closed its pipe"))

    def throttle(self, readers: list[asyncio.ReadTransport]) -> None:
        """Pause ``readers`` whenever the parent falls behind."""

        self._readers = readers
        if self._paused:
            for reader in readers:
                reader.pause_reading()

    def send(self, message: BaseModel) -> None:
        # After a loss the run fails on ``lost``; nothing more can reach the parent.
        if not self.lost.done():
            self._transport.write(encode_message(message))

    def close(self) -> None:
        """Flush what is buffered, then close: the parent then reads EOF."""

        self._closing = True
        self._transport.close()


class _CommandProtocol(asyncio.SubprocessProtocol):
    """Forward the command's output; learn its exit apart from its output's end.

    A process the command started can hold the pipes open after the command
    itself has exited, so the exit cannot wait for the output to end.
    """

    _transport: asyncio.SubprocessTransport

    def __init__(self, *, request_id: str, parent: _ParentOutput) -> None:
        loop = asyncio.get_running_loop()
        self._request_id = request_id
        self._parent = parent
        self._decoders = {
            fd: codecs.getincrementaldecoder("utf-8")(errors="replace") for fd in (1, 2)
        }
        self._seq = {1: 0, 2: 0}
        self._open_fds = {1, 2}
        self.exited: asyncio.Future[int] = loop.create_future()
        self.output_closed: asyncio.Future[None] = loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # asyncio holds pipe data and the exit until this has run.
        assert isinstance(transport, asyncio.SubprocessTransport)
        self._transport = transport
        self._parent.send(
            SandboxLifecycleEvent(
                request_id=self._request_id, event="started", at=_utc_now()
            )
        )
        readers = [transport.get_pipe_transport(fd) for fd in (1, 2)]
        self._parent.throttle(
            [reader for reader in readers if isinstance(reader, asyncio.ReadTransport)]
        )

    def _forward(self, fd: int, data: bytes) -> None:
        self._parent.send(
            SandboxOutputChunk(
                request_id=self._request_id,
                stream="stdout" if fd == 1 else "stderr",
                seq=self._seq[fd],
                data=self._decoders[fd].decode(data, final=not data),
            )
        )
        self._seq[fd] += 1

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        # One-shot sized chunks keep each message line short for the reader.
        for start in range(0, len(data), OUTPUT_CHUNK_SIZE_BYTES):
            self._forward(fd, data[start : start + OUTPUT_CHUNK_SIZE_BYTES])

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        # The empty chunk ends the stream, as in one-shot output.
        self._forward(fd, b"")
        self._open_fds.discard(fd)
        if not self._open_fds:
            self.output_closed.set_result(None)

    def process_exited(self) -> None:
        returncode = self._transport.get_returncode()
        assert returncode is not None  # Set before asyncio calls this.
        self.exited.set_result(returncode)


def _kill_command(transport: asyncio.SubprocessTransport) -> None:
    # The command may end between the check and the kill.
    with suppress(ProcessLookupError):
        transport.kill()


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
    transport: asyncio.SubprocessTransport,
    command: _CommandProtocol,
    grace_seconds: float,
) -> None:
    # The command shares this helper's group, which the broker's group kill and
    # startup recovery reach; the helper outlives SIGTERM (see main).
    os.killpg(os.getpgrp(), signal.SIGTERM)
    # Ends as soon as the command exits; output a descendant still holds open
    # gets only the drain window that follows every exit.
    await asyncio.wait((command.exited,), timeout=grace_seconds)
    if not command.exited.done():
        # Whatever else in the group ignored SIGTERM dies with the group kill
        # the parent sends after the exit report.
        _kill_command(transport)


async def _run_long_running(
    request: BrokerToSandboxLongRunningRequest, parent: _ParentOutput
) -> None:
    """Stream output until the command ends, the parent stops it, or temp is full."""

    try:
        transport, command = await asyncio.get_running_loop().subprocess_exec(
            lambda: _CommandProtocol(request_id=request.request_id, parent=parent),
            *_launch_argv(request),
            cwd=request.cwd,
            env=request.env,
            # The helper's own input carries the parent's stop requests.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        parent.send(
            SandboxProcessSpawnFailed(
                request_id=request.request_id, error=f"{type(error).__name__}: {error}"
            )
        )
        return
    temp_quota_task = asyncio.create_task(
        _monitor_temp_quota(
            temp_dir=Path(request.temp_dir),
            temp_storage_limit_bytes=request.temp_storage_limit_bytes,
            # Only the one-shot path waits on this event.
            budget_exceeded=asyncio.Event(),
        )
    )
    stop_task = asyncio.create_task(_read_stop_request(request.request_id))
    ended_by: Literal["command", "stop", "temp_storage_limit"] = "command"
    try:
        await asyncio.wait(
            (command.exited, temp_quota_task, stop_task, parent.lost),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if parent.lost.done():
            # Raises: the parent closed its pipe while the command ran.
            parent.lost.result()
        # A command that exited on its own ends by "command" whatever else woke.
        if not command.exited.done():
            if temp_quota_task.done():
                temp_quota_task.result()
                ended_by = "temp_storage_limit"
                _kill_command(transport)
            else:
                stop_task.result()
                ended_by = "stop"
                await _stop_command(
                    transport=transport,
                    command=command,
                    grace_seconds=request.stop_grace_ms / 1000,
                )
        returncode = await command.exited
        await asyncio.wait(
            (command.output_closed,), timeout=request.output_drain_ms / 1000
        )
    finally:
        for task in (temp_quota_task, stop_task):
            task.cancel()
        await asyncio.gather(temp_quota_task, stop_task, return_exceptions=True)
        # Nothing the group writes from here reaches the parent.
        transport.close()
    parent.send(
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


async def _report_failure(work: Awaitable[object]) -> None:
    try:
        await work
    except Exception:
        # This process boundary reports every helper failure: the broker shows
        # this stderr when the output ends without a completion receipt. A gone
        # broker must not end the helper here, before it can kill the group.
        with suppress(BrokenPipeError):
            traceback.print_exc()


async def _serve_request() -> None:
    raw_line = await asyncio.to_thread(sys.stdin.buffer.readline)
    request = decode_request(raw_line.decode("utf-8"))
    # EOF tells the broker the helper is done, receipt or not. The tracked group
    # leader then waits here for the broker to reap it.
    if isinstance(request, BrokerToSandboxLongRunningRequest):
        parent = await _ParentOutput.open()
        await _report_failure(_run_long_running(request, parent))
        parent.close()
    else:
        await _report_failure(_run_helper(request))
        # sys.stdout.close() would leave fd 1 open (the interpreter opens it
        # with closefd=False), so the broker would wait forever.
        os.close(sys.stdout.fileno())
    await asyncio.Future()


def _outlive_group_signals() -> None:
    """Keep the tracked group leader alive through what its group can send.

    A command that signals its own group (``kill 0``, a terminal-style SIGINT
    or SIGHUP) must not end the leader first: the resource tracker would then
    count the group as gone while the rest still runs. The broker ends the
    group with SIGKILL. Caught rather than ignored, so the command, which
    execs, starts with the default actions.
    """

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(signum, lambda: None)


async def main() -> int:
    _outlive_group_signals()
    async with asyncio.TaskGroup() as group:
        group.create_task(_kill_group_when_broker_exits())
        await _serve_request()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

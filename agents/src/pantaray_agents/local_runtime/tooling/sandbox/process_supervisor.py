"""Long-running Action commands that outlive the call that started them.

A run (its ``processes`` row: the Action's, or a subagent's) starts a command
here and gets back an opaque id; later calls of the same run read its new
output or stop it, and the run's end stops all of them. Each command runs in a
sandbox helper in long-running mode, with a temp dir (TMPDIR and HOME) of its
own and, when its network is on, an egress lease, held until it ends. Its
process group and temp dir are registered as resources the run owns, so the
periodic sweep reaps them if the run ends without stopping them.

Each Action job runs on its own event loop, which ends with the job, so the
supervisor owns a daemon thread and loop of its own, started on first use; its
state is touched only there.
"""

from __future__ import annotations

import asyncio
import codecs
import functools
import logging
import os
import shutil
import signal
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pantaray_agents.local_runtime.runtime.utc_timestamps import now_utc_iso

from ...storage.migrations import MigrationError
from ..action_session_temp_paths import create_private_temp_dir
from ..brokering.broker_protocol import ValidatedCommandRequest
from ..resources.resource_tracking import (
    register_path_resource,
    register_process_group_resource,
    release_run_owned_resource,
)
from . import runtime_policy
from .command_sandbox_client import spawn_sandbox_helper
from .command_sandbox_protocol import (
    SandboxLifecycleEvent,
    SandboxOutputChunk,
    SandboxProcessExited,
    SandboxProcessSpawnFailed,
    SandboxStopRequest,
    decode_long_running_message,
    encode_message,
)
from .command_sandbox_request import build_sandbox_request, to_long_running_request
from .egress_proxy import EgressLease, NetworkDestination, command_egress_lease
from .process_output import RetainedStream, StreamOutput

# No spaces: commands get the dir as TMPDIR and HOME.
PROCESS_TEMP_DIR_PREFIX = "pantaray-process-"

ProcessStatus = Literal[
    "running",
    "exited",
    "stopped",
    "limit_exceeded",
    "spawn_failed",
    # The helper ended without reporting how the command ended.
    "failed",
]

_ENDED_BY_STATUS: dict[str, ProcessStatus] = {
    "command": "exited",
    "stop": "stopped",
    "temp_storage_limit": "limit_exceeded",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessState:
    """A process as a call finds it: output since the previous call that
    returned it; byte totals and ``network`` over its whole life."""

    process_id: str
    # The call that started it, whose audit row the process's end belongs to.
    tool_invocation_id: str
    # What it was started with, which tells how a failure may have been blocked.
    request: ValidatedCommandRequest
    status: ProcessStatus
    exit_code: int | None
    signal: int | None
    # Why it could not start (spawn_failed) or what failed (failed).
    error: str | None
    stdout: StreamOutput
    stderr: StreamOutput
    stdout_bytes: int
    stderr_bytes: int
    # Empty when its network is off.
    network: tuple[NetworkDestination, ...]


class UnknownProcessError(LookupError):
    """No process of this run has the id."""

    def __init__(self, process_id: str, running_ids: tuple[str, ...]) -> None:
        super().__init__(f"no process {process_id!r} in this run")
        self.process_id = process_id
        self.running_ids = running_ids


class ProcessRunEndingError(RuntimeError):
    """The run is stopping its processes; it starts no more."""


class ProcessLimitError(RuntimeError):
    """The run already has as many running processes as it may."""

    def __init__(self, running_ids: tuple[str, ...]) -> None:
        super().__init__(f"this run already has {len(running_ids)} running processes")
        self.running_ids = running_ids


@dataclass(frozen=True, slots=True)
class _End:
    status: ProcessStatus
    exit_code: int | None = None
    signal: int | None = None
    error: str | None = None


@dataclass(eq=False, slots=True)
class _Process:
    process_id: str
    tool_invocation_id: str
    request: ValidatedCommandRequest
    request_id: str
    helper: asyncio.subprocess.Process
    lease: EgressLease | None
    # Unwinds everything held for the process: group, lease, temp dir.
    resources: AsyncExitStack
    stdout: RetainedStream
    stderr: RetainedStream
    # Set once the helper has started the command, or failed to.
    started: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    end: _End | None = None
    stop_sent: bool = False
    # Set when the supervisor killed a helper that did not report in time.
    unreported_error: str | None = None
    # Held so the loop, which keeps tasks only weakly, does not drop it.
    watcher: asyncio.Task[None] | None = None

    def take_state(self) -> ProcessState:
        end = self.end or _End(status="running")
        return ProcessState(
            process_id=self.process_id,
            tool_invocation_id=self.tool_invocation_id,
            request=self.request,
            status=end.status,
            exit_code=end.exit_code,
            signal=end.signal,
            error=end.error,
            stdout=self.stdout.take(),
            stderr=self.stderr.take(),
            stdout_bytes=self.stdout.total_bytes,
            stderr_bytes=self.stderr.total_bytes,
            network=() if self.lease is None else self.lease.destinations(),
        )


def _running_ids(processes: dict[str, _Process]) -> tuple[str, ...]:
    return tuple(pid for pid, process in processes.items() if process.end is None)


async def _release(*, db_path: Path, busy_timeout_ms: int, resource_id: str) -> None:
    try:
        await asyncio.to_thread(
            release_run_owned_resource,
            db_path=db_path,
            busy_timeout_ms=busy_timeout_ms,
            resource_id=resource_id,
            released_at=now_utc_iso(),
        )
    except (sqlite3.Error, MigrationError):
        # The row stays active; the sweep reaps it once the run has ended.
        logger.exception("failed to release run-owned resource %s", resource_id)


async def _kill_helper_group(helper: asyncio.subprocess.Process) -> None:
    # A group left with only unreaped exits answers EPERM on macOS.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(helper.pid, signal.SIGKILL)
    await helper.wait()


async def _read_messages(process: _Process) -> _End:
    assert process.helper.stdout is not None
    try:
        while line := await process.helper.stdout.readline():
            message = decode_long_running_message(line.decode("utf-8"))
            if isinstance(message, SandboxLifecycleEvent):
                process.started.set()
            elif isinstance(message, SandboxOutputChunk):
                stream = (
                    process.stdout if message.stream == "stdout" else process.stderr
                )
                stream.append(message.data)
            elif isinstance(message, SandboxProcessExited):
                return _End(
                    status=_ENDED_BY_STATUS[message.ended_by],
                    exit_code=message.exit_code,
                    signal=message.signal,
                )
            elif isinstance(message, SandboxProcessSpawnFailed):
                return _End(status="spawn_failed", error=message.error)
    except ValueError as exc:
        return _End(status="failed", error=f"invalid command sandbox message: {exc}")
    return _End(
        status="failed",
        error=process.unreported_error
        or "command sandbox ended without reporting the command's end",
    )


async def _read_helper_stderr(process: _Process) -> None:
    """The helper's own errors, after whatever the command wrote to stderr."""

    assert process.helper.stderr is not None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while chunk := await process.helper.stderr.read(4096):
        process.stderr.append(decoder.decode(chunk))
    process.stderr.append(decoder.decode(b"", final=True))


async def _watch(process: _Process) -> None:
    """Drain the helper until its final report, then let go of everything held."""

    stderr_reader = asyncio.create_task(_read_helper_stderr(process))
    end = await _read_messages(process)
    try:
        await process.resources.aclose()
        await stderr_reader
    finally:
        # Readers and stops wait on this; an error above still ends the task.
        process.end = end
        process.started.set()
        process.ended.set()


async def _stop(process: _Process) -> None:
    if process.end is None and not process.stop_sent:
        process.stop_sent = True
        assert process.helper.stdin is not None
        try:
            process.helper.stdin.write(
                encode_message(SandboxStopRequest(request_id=process.request_id))
            )
            await process.helper.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The helper is gone; its watcher records how it ended.
    # A command can freeze the helper (SIGSTOP to their shared group).
    deadline_ms = (
        runtime_policy.PROCESS_STOP_GRACE_MS
        + runtime_policy.PROCESS_OUTPUT_DRAIN_MS
        + runtime_policy.PROCESS_STOP_REPORT_MARGIN_MS
    )
    with suppress(TimeoutError):
        async with asyncio.timeout(deadline_ms / 1000):
            await process.ended.wait()
            return
    # Only while unreaped: a reaped helper's pid may belong to someone else.
    if process.helper.returncode is None:
        process.unreported_error = (
            f"command sandbox did not report the stop within {deadline_ms} ms"
        )
        # Also ends a stopped process; the watcher, seeing the output end,
        # records and releases it as for any helper death.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.helper.pid, signal.SIGKILL)
    await process.ended.wait()


class _ProcessSupervisor:
    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._thread_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Per owner process id, its processes by id. Supervisor loop only.
        # Design limit: ended processes stay, unread output included, until the
        # run ends; forget them on read if runs are seen to start hundreds.
        self._owners: dict[str, dict[str, _Process]] = {}
        # Serializes launches (for the per-run limit) and a run's end.
        self._start_lock = asyncio.Lock()
        self._closing: set[str] = set()  # Owners whose run end is stopping.
        # Per owner, the run-end stop in progress, which later calls join.
        self._stopping: dict[str, asyncio.Future[tuple[ProcessState, ...]]] = {}
        self._background: set[asyncio.Task[None]] = set()

    def ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._thread_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="process-supervisor", daemon=True
                ).start()
                self._loop = loop
            return self._loop

    def _find(self, owner_process_id: str, process_id: str) -> _Process:
        processes = self._owners.get(owner_process_id, {})
        process = processes.get(process_id)
        if process is None:
            raise UnknownProcessError(process_id, _running_ids(processes))
        return process

    async def start(
        self,
        *,
        db_path: Path,
        busy_timeout_ms: int,
        owner_process_id: str,
        request: ValidatedCommandRequest,
    ) -> ProcessState:
        """Start the command for the run: ``running``, or ended within the window.

        Raises ``ProcessLimitError`` at ``PROCESS_RUN_LIMIT``,
        ``ProcessRunEndingError`` while the run's end stops its processes,
        ``EgressProxyStartError`` when its network cannot be set up, and
        ``ToolRuntimeResourceSessionConflictError`` when the run or the
        starting call no longer runs. Cancelling the start stops the process."""

        # The launch is not cancelled with the caller, which could otherwise
        # interrupt it between spawning the helper and recording it.
        launch = asyncio.ensure_future(
            self._launch(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                owner_process_id=owner_process_id,
                request=request,
            )
        )
        try:
            process = await asyncio.shield(launch)
            # The window is the command's, not the helper's own start-up.
            await process.started.wait()
            with suppress(TimeoutError):
                async with asyncio.timeout(
                    runtime_policy.PROCESS_START_WINDOW_MS / 1000
                ):
                    await process.ended.wait()
        except asyncio.CancelledError:
            # Nobody learns the id of a process whose start was cancelled.
            launch.add_done_callback(self._stop_abandoned)
            raise
        return process.take_state()

    def _stop_abandoned(self, launch: asyncio.Future[_Process]) -> None:
        if launch.exception() is None:
            self._run_in_background(_stop(launch.result()))

    def _run_in_background(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _launch(
        self,
        *,
        db_path: Path,
        busy_timeout_ms: int,
        owner_process_id: str,
        request: ValidatedCommandRequest,
    ) -> _Process:
        tool_invocation_id = request.tool_invocation_id
        if tool_invocation_id is None:
            raise ValueError("a long-running process needs the call that starts it")
        async with self._start_lock, AsyncExitStack() as held:
            if owner_process_id in self._closing:
                raise ProcessRunEndingError(f"run {owner_process_id!r} is ending")
            processes = self._owners.setdefault(owner_process_id, {})
            running_ids = _running_ids(processes)
            if len(running_ids) >= runtime_policy.PROCESS_RUN_LIMIT:
                raise ProcessLimitError(running_ids)
            temp_dir = create_private_temp_dir(
                db_path=db_path, prefix=PROCESS_TEMP_DIR_PREFIX
            )
            # Covers a registration that fails; a no-op after the release.
            held.callback(shutil.rmtree, temp_dir, ignore_errors=True)
            temp_resource_id = register_path_resource(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                execution_session_id=request.execution_session_id,
                tool_invocation_id=tool_invocation_id,
                action_id=request.action_id,
                resource_kind="temp_dir",
                resource_path=temp_dir,
                created_at=request.requested_at,
                owner_process_id=owner_process_id,
            )
            assert temp_resource_id is not None
            held.push_async_callback(
                _release,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                resource_id=temp_resource_id,
            )
            lease = (
                await held.enter_async_context(command_egress_lease())
                if request.network_policy == "allow"
                else None
            )
            sandbox_request = to_long_running_request(
                build_sandbox_request(request=request, temp_dir=temp_dir, egress=lease),
                stop_grace_ms=runtime_policy.PROCESS_STOP_GRACE_MS,
                output_drain_ms=runtime_policy.PROCESS_OUTPUT_DRAIN_MS,
            )
            helper = await spawn_sandbox_helper(asyncio.create_subprocess_exec)
            held.push_async_callback(_kill_helper_group, helper)
            group_resource_id = register_process_group_resource(
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                execution_session_id=request.execution_session_id,
                tool_invocation_id=tool_invocation_id,
                action_id=request.action_id,
                pid=helper.pid,
                created_at=request.requested_at,
                owner_process_id=owner_process_id,
            )
            assert group_resource_id is not None
            held.push_async_callback(
                _release,
                db_path=db_path,
                busy_timeout_ms=busy_timeout_ms,
                resource_id=group_resource_id,
            )
            process_id = f"p{len(processes) + 1}"
            process = _Process(
                process_id=process_id,
                tool_invocation_id=tool_invocation_id,
                request=request,
                request_id=sandbox_request.request_id,
                helper=helper,
                lease=lease,
                resources=held.pop_all(),
                stdout=RetainedStream(runtime_policy.PROCESS_RETAINED_OUTPUT_BYTES),
                stderr=RetainedStream(runtime_policy.PROCESS_RETAINED_OUTPUT_BYTES),
            )
            # Written before the process can be found, so before any stop. The
            # helper's stdin stays open: a stop is written to it.
            assert helper.stdin is not None
            helper.stdin.write(encode_message(sandbox_request))
            process.watcher = asyncio.create_task(_watch(process))
            processes[process_id] = process
        try:
            await helper.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # The helper is gone; its watcher records how it ended.
        return process

    async def read(
        self, *, owner_process_id: str, process_id: str, wait_seconds: float
    ) -> ProcessState:
        """New output and the state, once it ends or ``wait_seconds`` pass.

        Raises ``UnknownProcessError`` for an id this run does not have.
        """

        process = self._find(owner_process_id, process_id)
        # A cancelled wait takes nothing: the output stays for the next read.
        with suppress(TimeoutError):
            async with asyncio.timeout(wait_seconds):
                await process.ended.wait()
        return process.take_state()

    async def stop(self, *, owner_process_id: str, process_id: str) -> ProcessState:
        """SIGTERM, then SIGKILL after the grace; returns how it ended.

        A process that already ended is returned as it is. Raises
        ``UnknownProcessError`` for an id this run does not have.
        """

        process = self._find(owner_process_id, process_id)
        await _stop(process)
        return process.take_state()

    async def stop_owner(self, *, owner_process_id: str) -> tuple[ProcessState, ...]:
        """At the run's end: stop what it still runs and forget all of it.

        Returns every process the run started, ended, in start order. A call
        made while another stops the same run waits for that stop and returns
        what it returns.
        """

        stopping = self._stopping.get(owner_process_id)
        if stopping is None:
            stopping = asyncio.ensure_future(self._stop_owner(owner_process_id))
            self._stopping[owner_process_id] = stopping
            stopping.add_done_callback(
                lambda _: self._stopping.pop(owner_process_id, None)
            )
        # Not cancelled with the caller: nothing would stop them after the pop.
        return await asyncio.shield(stopping)

    async def _stop_owner(self, owner_process_id: str) -> tuple[ProcessState, ...]:
        async with self._start_lock:
            processes = self._owners.pop(owner_process_id, {})
            self._closing.add(owner_process_id)
        try:
            await asyncio.gather(*(_stop(process) for process in processes.values()))
        finally:
            self._closing.discard(owner_process_id)
        return tuple(process.take_state() for process in processes.values())


_SUPERVISOR = _ProcessSupervisor()
# A forked child has the state but not the thread that runs its loop.
os.register_at_fork(after_in_child=_SUPERVISOR._reset)


def _on_supervisor_loop[**P, T](
    method: Callable[P, Coroutine[object, object, T]],
) -> Callable[P, Coroutine[object, object, T]]:
    """Run the supervisor's ``method`` on its loop; cancelling the caller cancels it."""

    @functools.wraps(method)
    async def call(*args: P.args, **kwargs: P.kwargs) -> T:
        loop = _SUPERVISOR.ensure_loop()
        future = asyncio.run_coroutine_threadsafe(method(*args, **kwargs), loop)
        return await asyncio.wrap_future(future)

    return call


start_process = _on_supervisor_loop(_SUPERVISOR.start)
read_process_output = _on_supervisor_loop(_SUPERVISOR.read)
stop_process = _on_supervisor_loop(_SUPERVISOR.stop)
stop_owner_processes = _on_supervisor_loop(_SUPERVISOR.stop_owner)


__all__ = [
    "ProcessLimitError",
    "ProcessRunEndingError",
    "ProcessState",
    "ProcessStatus",
    "StreamOutput",
    "UnknownProcessError",
    "read_process_output",
    "start_process",
    "stop_owner_processes",
    "stop_process",
]

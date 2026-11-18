"""Long-running processes driven through the supervisor on real sandbox helpers."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.unit.local_runtime.resource_recovery_test_support import (
    bootstrap_runtime_db,
    register_running_bash_invocation,
)
from tests.unit.local_runtime.test_run_owned_resources import (
    ACTION_PROCESS_ID,
    SUBAGENT_PROCESS_ID,
    _insert_run_processes,
)
from tests.unit.local_runtime.test_seatbelt_profiles import _validated_command_request

from pantaray_agents.local_runtime.tooling.resources.resource_cleanup import (
    process_exists,
)
from pantaray_agents.local_runtime.tooling.sandbox import egress_proxy, runtime_policy
from pantaray_agents.local_runtime.tooling.sandbox.egress_proxy import (
    NetworkDestination,
)
from pantaray_agents.local_runtime.tooling.sandbox.process_output import (
    RetainedStream,
    StreamOutput,
)
from pantaray_agents.local_runtime.tooling.sandbox.process_supervisor import (
    ProcessLimitError,
    ProcessState,
    UnknownProcessError,
    read_process_output,
    start_process,
    stop_owner_processes,
    stop_process,
)

BUSY_TIMEOUT_MS = 1_000
# Bounds every wait on the supervisor; none should take more than a few seconds.
WAIT_TIMEOUT_S = 20
LOOP_UNTIL_STOPPED = "while :; do sleep 0.05; done"


@dataclass(frozen=True, slots=True)
class _Run:
    db_path: Path
    execution_session_id: str
    invocation_id: str
    workspace: Path

    async def start(
        self,
        script: str,
        *,
        network_policy: str = "deny",
        argv: list[str] | None = None,
    ) -> ProcessState:
        request = _validated_command_request(network_policy=network_policy)
        start = start_process(
            db_path=self.db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            owner_process_id=ACTION_PROCESS_ID,
            request=request.model_copy(
                update={
                    "tool_invocation_id": self.invocation_id,
                    "execution_session_id": self.execution_session_id,
                    "app_runtime_python": sys.executable,
                    "cwd": str(self.workspace),
                    "argv": argv or ["/bin/sh", "-c", script],
                    "env": {"PATH": "/usr/bin:/bin"},
                    # Skips seatbelt only: the helper's other limits stay.
                    "run_outside_sandbox": True,
                }
            ),
        )
        return await asyncio.wait_for(start, WAIT_TIMEOUT_S)

    async def read(self, process_id: str, wait_seconds: float = 0) -> ProcessState:
        read = read_process_output(
            owner_process_id=ACTION_PROCESS_ID,
            process_id=process_id,
            wait_seconds=wait_seconds,
        )
        return await asyncio.wait_for(read, WAIT_TIMEOUT_S + wait_seconds)

    async def read_until(self, process_id: str, text: str, seen: str = "") -> str:
        """Read until ``text`` is in ``seen`` and what followed it; returns both."""

        async with asyncio.timeout(WAIT_TIMEOUT_S):
            while text not in seen:
                seen += (await self.read(process_id, wait_seconds=0.05)).stdout.text
        return seen

    async def stop(self, process_id: str) -> ProcessState:
        stop = stop_process(owner_process_id=ACTION_PROCESS_ID, process_id=process_id)
        return await asyncio.wait_for(stop, WAIT_TIMEOUT_S)

    def resources(self) -> list[tuple[str, str, str | None]]:
        with sqlite3.connect(self.db_path) as connection:
            return connection.execute(
                "SELECT resource_kind, status, resource_path "
                "FROM tool_runtime_resources WHERE owner_process_id = ?",
                (ACTION_PROCESS_ID,),
            ).fetchall()

    def assert_released(self) -> None:
        resources = self.resources()
        assert sorted(kind for kind, _, _ in resources) == ["process_group", "temp_dir"]
        assert {status for _, status, _ in resources} == {"cleaned"}
        assert not any(Path(path).exists() for _, _, path in resources if path)


@pytest.fixture
def fast_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_policy, "PROCESS_START_WINDOW_MS", 300)
    monkeypatch.setattr(runtime_policy, "PROCESS_STOP_GRACE_MS", 300)
    monkeypatch.setattr(runtime_policy, "PROCESS_OUTPUT_DRAIN_MS", 100)


@pytest.fixture
async def run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_policy: None
) -> AsyncIterator[_Run]:
    # Command temp dirs go under ~/Library/Caches.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db_path, context = bootstrap_runtime_db(tmp_path)
    _insert_run_processes(db_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        yield _Run(
            db_path=db_path,
            execution_session_id=context.execution_session_id,
            invocation_id=register_running_bash_invocation(
                db_path=db_path, context=context
            ),
            workspace=workspace,
        )
    finally:
        async with asyncio.timeout(WAIT_TIMEOUT_S):
            await stop_owner_processes(owner_process_id=ACTION_PROCESS_ID)


async def test_a_command_that_ends_in_the_start_window_returns_its_end(
    run: _Run,
) -> None:
    state = await run.start("echo out; echo err >&2; exit 3")

    assert (state.status, state.exit_code, state.signal) == ("exited", 3, None)
    assert state.stdout == StreamOutput(text="out\n", dropped_bytes=0)
    assert state.stderr.text == "err\n"
    run.assert_released()


async def test_reads_return_only_new_output_and_a_cancelled_read_leaves_the_process(
    run: _Run,
) -> None:
    started = await run.start(
        "trap 'exit 7' TERM; echo one; "
        f"while [ ! -e go ]; do sleep 0.05; done; echo two; {LOOP_UNTIL_STOPPED}"
    )
    assert started.status == "running"
    seen = await run.read_until(started.process_id, "one", started.stdout.text)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run.read(started.process_id, wait_seconds=30), 0.3)
    (run.workspace / "go").touch()
    seen = await run.read_until(started.process_id, "two", seen)
    stopped = await run.stop(started.process_id)
    again = await run.stop(started.process_id)

    assert seen == "one\ntwo\n"
    assert (stopped.status, stopped.exit_code) == ("stopped", 7)
    assert (again.status, again.exit_code, again.stdout.text) == ("stopped", 7, "")
    run.assert_released()


async def test_a_read_returns_as_soon_as_the_process_ends(run: _Run) -> None:
    started = await run.start("echo ready; sleep 1; echo bye; exit 4")
    assert started.status == "running"

    began = time.monotonic()
    ended = await run.read(started.process_id, wait_seconds=20)

    assert time.monotonic() - began < 10
    assert (ended.status, ended.exit_code) == ("exited", 4)
    assert started.stdout.text + ended.stdout.text == "ready\nbye\n"


async def test_output_past_the_cap_keeps_its_ends_and_the_process_runs_on(
    run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = 1024
    monkeypatch.setattr(runtime_policy, "PROCESS_RETAINED_OUTPUT_BYTES", cap)
    size = 1_000_000
    started = await run.start(
        f"head -c {size} /dev/zero | tr '\\0' a; echo END; {LOOP_UNTIL_STOPPED}"
    )
    states = [started]
    async with asyncio.timeout(WAIT_TIMEOUT_S):
        while states[-1].stdout_bytes < size + 4:
            states.append(await run.read(started.process_id, wait_seconds=0.05))

    kept = dropped = 0
    for state in states:
        output = state.stdout
        marker = (
            f"\n[... {output.dropped_bytes} bytes of output dropped ...]\n"
            if output.dropped_bytes
            else ""
        )
        assert marker in output.text
        assert len(output.text) - len(marker) <= cap
        kept += len(output.text) - len(marker)
        dropped += output.dropped_bytes
    assert states[-1].status == "running"
    assert states[0].stdout.text.startswith("a" * (cap // 2))
    assert "".join(state.stdout.text for state in states).endswith("aEND\n")
    assert dropped > 0
    assert kept + dropped == states[-1].stdout_bytes == size + 4


def test_retention_cuts_between_whole_characters() -> None:
    stream = RetainedStream(cap_bytes=8)
    stream.append("ああ")
    stream.append("いいい")

    assert stream.total_bytes == 15
    assert stream.take() == StreamOutput(
        text="あ\n[... 9 bytes of output dropped ...]\nい", dropped_bytes=9
    )
    assert stream.take() == StreamOutput(text="", dropped_bytes=0)


async def test_a_run_over_its_limit_is_refused_and_sees_only_its_own_processes(
    run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_policy, "PROCESS_RUN_LIMIT", 1)
    first = await run.start(LOOP_UNTIL_STOPPED)

    with pytest.raises(ProcessLimitError) as refused:
        await run.start("echo never")
    with pytest.raises(UnknownProcessError) as other_run:
        await asyncio.wait_for(
            read_process_output(
                owner_process_id=SUBAGENT_PROCESS_ID,
                process_id=first.process_id,
                wait_seconds=0,
            ),
            WAIT_TIMEOUT_S,
        )
    with pytest.raises(UnknownProcessError) as unknown:
        await run.read("p9")
    still = await run.read(first.process_id)
    ended = await asyncio.wait_for(
        stop_owner_processes(owner_process_id=ACTION_PROCESS_ID), WAIT_TIMEOUT_S
    )

    assert refused.value.running_ids == (first.process_id,)
    assert other_run.value.running_ids == ()
    assert unknown.value.running_ids == (first.process_id,)
    assert still.status == "running"
    assert [(state.process_id, state.status) for state in ended] == [
        (first.process_id, "stopped")
    ]
    with pytest.raises(UnknownProcessError):
        await run.read(first.process_id)
    run.assert_released()


async def test_a_command_that_cannot_start_is_reported(run: _Run) -> None:
    state = await run.start("", argv=[str(run.workspace / "missing-executable")])

    assert state.status == "spawn_failed"
    assert state.error is not None and "FileNotFoundError" in state.error
    run.assert_released()


async def test_cancelling_a_start_stops_the_process(
    run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_policy, "PROCESS_START_WINDOW_MS", 10_000)
    pid_file = run.workspace / "command.pid"

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(run.start(f'echo $$ > "{pid_file}"; exec sleep 60'), 1)
    async with asyncio.timeout(WAIT_TIMEOUT_S):
        while not pid_file.exists() or not pid_file.read_text().strip():
            await asyncio.sleep(0.05)
        while process_exists(int(pid_file.read_text())):
            await asyncio.sleep(0.05)
        while {status for _, status, _ in run.resources()} != {"cleaned"}:
            await asyncio.sleep(0.05)


@asynccontextmanager
async def _http_server() -> AsyncIterator[int]:
    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


async def test_the_network_lease_lasts_as_long_as_the_process(
    run: _Run, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(egress_proxy, "is_refused_address", lambda _address: False)

    async with _http_server() as port:
        started = await run.start(
            # After the start window, so after the starting call returned.
            "sleep 0.6; "
            f"curl -s --noproxy '' -x \"$HTTP_PROXY\" http://127.0.0.1:{port}/; "
            f"echo; echo fetched; {LOOP_UNTIL_STOPPED}",
            network_policy="allow",
        )
        assert started.status == "running"
        seen = await run.read_until(started.process_id, "fetched")
        stopped = await run.stop(started.process_id)

    assert seen.startswith("ok")
    assert [(d.host, d.port, d.connections, d.failed) for d in stopped.network] == [
        ("127.0.0.1", port, 1, 0)
    ]
    assert isinstance(stopped.network[0], NetworkDestination)

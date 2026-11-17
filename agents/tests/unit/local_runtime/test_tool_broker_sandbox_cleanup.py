from __future__ import annotations

import asyncio
import json
import signal
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    SandboxOutputChunk,
    encode_message,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _stub_process_group_resource_registration,
    _stub_runtime_budget,
)


@pytest.mark.asyncio
async def test_execute_broker_tool_cleans_process_resources_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)

    class _FakeStdout:
        def __init__(self) -> None:
            self._messages: list[bytes] = []

        def prime(self, request_id: str) -> None:
            self._messages = [
                encode_message(
                    SandboxLifecycleEvent(
                        request_id=request_id,
                        event="started",
                        at="2026-03-23T00:00:00Z",
                    )
                ),
                encode_message(
                    SandboxLifecycleEvent(
                        request_id=request_id,
                        event="timeout_sent",
                        at="2026-03-23T00:00:01Z",
                    )
                ),
                encode_message(
                    SandboxCommandCompletion(
                        request_id=request_id,
                        outcome="timed_out",
                        exit_code=None,
                        signal=signal.SIGKILL,
                        stdout_bytes=0,
                        stderr_bytes=0,
                    )
                ),
            ]

        async def readline(self) -> bytes:
            if not self._messages:
                return b""
            return self._messages.pop(0)

    class _FakeStderr:
        async def read(self) -> bytes:
            return b""

    class _FakeStdin:
        def __init__(self, stdout_pipe: _FakeStdout) -> None:
            self._stdout_pipe = stdout_pipe

        def write(self, data: bytes) -> None:
            payload = json.loads(data.decode("utf-8"))
            self._stdout_pipe.prime(payload["request_id"])

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 9876
        returncode = 0

        def __init__(self) -> None:
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()
            self.stdin = _FakeStdin(self.stdout)

        async def wait(self) -> int:
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    _stub_process_group_resource_registration(monkeypatch)
    _stub_runtime_budget(monkeypatch, timeout_ms=1)

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-bash-timeout-kill-failure",
        args={"command": "pwd"},
    )

    with sqlite3.connect(db_path) as connection:
        resource_rows = connection.execute(
            """
            SELECT status
            FROM tool_runtime_resources
            WHERE resource_kind = 'process_group'
            """
        ).fetchall()

    assert outcome.status == "error"
    assert outcome.output["error"]["type"] == "ToolTimeoutError"
    assert resource_rows == [("cleaned",)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "expected_type", "expected_exit", "expected_audit"),
    [
        ("exited", "CommandExecutionError", 4, "exited"),
        ("signaled", "CommandExecutionError", -1, "signaled"),
        ("timed_out", "ToolTimeoutError", -1, "timed_out"),
        ("canceled", "CommandResourceLimitExceededError", -1, "budget_exceeded"),
        ("spawn_failed", "CommandExecutionError", -1, "spawn_failed"),
        (None, "CommandSandboxError", -1, "broker_failed"),
        ("invalid", "CommandSandboxError", -1, "broker_failed"),
        ("send_failed", "CommandSandboxError", -1, "broker_failed"),
    ],
)
async def test_command_failures_keep_captured_output_and_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str | None,
    expected_type: str,
    expected_exit: int,
    expected_audit: str,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)

    raw_violation = (
        "ModuleNotFoundError: command sandbox dependency is missing\n"
        if terminal == "send_failed"
        else "sandbox fixture: execution assertion failed\n"
    )
    partial_stdout = "" if terminal == "send_failed" else "completed step 1\n"

    class _FakeStdout:
        def __init__(self) -> None:
            self._messages: list[bytes] = []

        def prime(self, request_id: str) -> None:
            if terminal == "send_failed":
                return
            self._messages = [
                encode_message(
                    SandboxLifecycleEvent(
                        request_id=request_id,
                        event="started",
                        at="2026-03-23T00:00:00Z",
                    )
                ),
                encode_message(
                    SandboxOutputChunk(
                        request_id=request_id,
                        stream="stderr",
                        seq=0,
                        data=raw_violation,
                    )
                ),
                encode_message(
                    SandboxOutputChunk(
                        request_id=request_id,
                        stream="stdout",
                        seq=0,
                        data=partial_stdout,
                    )
                ),
            ]
            if terminal == "invalid":
                self._messages.append(b"not-json\n")
            elif terminal is not None:
                self._messages.append(
                    encode_message(
                        SandboxCommandCompletion(
                            request_id=request_id,
                            outcome=terminal,
                            exit_code=4 if terminal == "exited" else None,
                            signal=9
                            if terminal in {"signaled", "timed_out", "canceled"}
                            else None,
                            stdout_bytes=len(partial_stdout.encode("utf-8")),
                            stderr_bytes=len(raw_violation.encode("utf-8")),
                            budget_exceeded_kind="stdout_limit"
                            if terminal == "canceled"
                            else None,
                        )
                    )
                )

        async def readline(self) -> bytes:
            if not self._messages:
                return b""
            return self._messages.pop(0)

    class _FakeStderr:
        async def read(self) -> bytes:
            return raw_violation.encode() if terminal == "send_failed" else b""

    class _FakeStdin:
        def __init__(self, stdout_pipe: _FakeStdout) -> None:
            self._stdout_pipe = stdout_pipe

        def write(self, data: bytes) -> None:
            payload = json.loads(data.decode("utf-8"))
            self._stdout_pipe.prime(payload["request_id"])

        async def drain(self) -> None:
            if terminal == "send_failed":
                raise BrokenPipeError("helper exited before consuming the request")
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 9999
        returncode = 0

        def __init__(self) -> None:
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()
            self.stdin = _FakeStdin(self.stdout)

        async def wait(self) -> int:
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    _stub_process_group_resource_registration(monkeypatch)

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-bash-sandbox-violation",
        args={"command": "pwd"},
    )

    with sqlite3.connect(db_path) as connection:
        audit_row = connection.execute(
            """
            SELECT terminal_outcome, exit_code, sandbox_violation_summary
            FROM command_invocation_audits
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert outcome.status == "error"
    assert outcome.output["stdout"] == partial_stdout
    assert outcome.output["stderr"] == raw_violation
    assert outcome.output["exit_code"] == expected_exit
    assert outcome.output["error"]["type"] == expected_type
    assert audit_row == (expected_audit, 4 if terminal == "exited" else None, None)
    if terminal in {"signaled", "timed_out", "canceled"}:
        assert outcome.output["signal"] == 9
    if terminal == "canceled":
        assert outcome.output["error"]["code"] == "stdout_limit"
    with sqlite3.connect(db_path) as connection:
        stored_output = json.loads(
            connection.execute(
                "SELECT output_json FROM tool_outputs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert stored_output["stdout"] == partial_stdout
    assert stored_output["stderr"] == raw_violation


@pytest.mark.asyncio
async def test_execute_broker_tool_preserves_cancellation_when_process_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)
    started = asyncio.Event()

    class _FakeStdout:
        async def readline(self) -> bytes:
            started.set()
            await asyncio.Future()
            return b""

    class _FakeStderr:
        async def read(self) -> bytes:
            return b""

    class _FakeStdin:
        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 2467
        returncode = None

        def __init__(self) -> None:
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()
            self.stdin = _FakeStdin()

        async def wait(self) -> int:
            self.returncode = -9
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    _stub_process_group_resource_registration(monkeypatch)
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.cleanup.os.killpg",
        lambda _pid, _sig: (_ for _ in ()).throw(OSError("kill failed")),
    )
    # The fake pid may belong to a real process on the host; the premise here is
    # that the tracked process is gone.
    for target in (
        "pantaray_agents.local_runtime.tooling.resources.resource_cleanup.process_exists",
        "pantaray_agents.local_runtime.tooling.resources.process_identity._process_exists",
    ):
        monkeypatch.setattr(target, lambda _pid: False)
    _stub_runtime_budget(monkeypatch, timeout_ms=10_000)

    task = asyncio.create_task(
        execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-cancel-kill-failure",
            args={"command": "pwd"},
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    with sqlite3.connect(db_path) as connection:
        resource_rows = connection.execute(
            """
            SELECT status
            FROM tool_runtime_resources
            WHERE resource_kind = 'process_group'
            """
        ).fetchall()
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE tool_request_id = ?",
            ("request-bash-cancel-kill-failure",),
        ).fetchone()

    # The kill failure is recorded, then finalizing the canceled invocation
    # reconciles the resource because the tracked process is verifiably gone.
    assert resource_rows == [("cleaned",)]
    assert invocation_row == ("canceled",)

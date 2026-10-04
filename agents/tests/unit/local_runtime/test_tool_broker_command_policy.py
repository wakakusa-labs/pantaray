from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_command_validation import (
    build_validated_command_request,
    build_validated_python_request,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import (
    load_broker_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BashToolArgs,
    RunPythonToolArgs,
    ValidatedCommandRequest,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    encode_message,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)


@pytest.mark.asyncio
async def test_execute_broker_tool_uses_allowlisted_env_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    monkeypatch.setenv("BASH_ENV", str(context.workspace_path / "should_not_run.sh"))
    captured_env: dict[str, str] = {}

    class _FakeStdout:
        def __init__(self) -> None:
            self._messages: list[bytes] = []

        def prime(self, *, request_id: str) -> None:
            self._messages = [
                encode_message(
                    SandboxLifecycleEvent(
                        request_id=request_id,
                        event="started",
                        at="2026-03-23T00:00:00Z",
                    )
                ),
                encode_message(
                    SandboxCommandCompletion(
                        request_id=request_id,
                        outcome="exited",
                        exit_code=0,
                        signal=None,
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
            captured_env.update(payload["env"])
            self._stdout_pipe.prime(request_id=payload["request_id"])

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 123
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

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-bash-env",
        args={"command": "pwd"},
    )

    assert outcome.status == "success"
    assert "BASH_ENV" not in captured_env


@pytest.mark.asyncio
async def test_shell_input_rejects_nul_before_process_launch(tmp_path: Path) -> None:
    from pantaray_agents.local_runtime.tooling.brokering.broker import BrokerPolicyError

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    with pytest.raises(BrokerPolicyError, match="must not contain NUL"):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-shell-nul",
            args={"command": "printf before\x00after"},
        )


@pytest.mark.parametrize("read_access_scope", ["workspace", "full_access"])
def test_command_reads_follow_setting_and_login_leaves_roots_unchanged(
    tmp_path: Path, read_access_scope: str
) -> None:
    db_path, context = _bootstrap_runtime_db(
        tmp_path, read_access_scope=read_access_scope
    )
    _grant_workspace_full_access(db_path=db_path, capability="process_exec_local")
    broker_context = load_broker_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        path_access_kind="exec",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
    )

    def validate(*, use_login_environment: bool) -> ValidatedCommandRequest:
        return build_validated_command_request(
            context=broker_context,
            args=BashToolArgs(
                command="gh auth status",
                use_login_environment=use_login_environment,
                justification=(
                    "Check your GitHub sign-in." if use_login_environment else None
                ),
            ),
            tool_invocation_id=None,
            tool_request_id=f"request-login-{use_login_environment}",
            requested_at="2026-03-23T00:00:02Z",
            preflight_only=True,
        )

    normal = validate(use_login_environment=False)
    login = validate(use_login_environment=True)

    assert ("/" in normal.real_read_roots) is (read_access_scope == "full_access")
    assert login.real_read_roots == normal.real_read_roots
    assert login.real_write_roots == normal.real_write_roots
    assert login.private_storage_roots == normal.private_storage_roots
    assert login.command_summary_json["use_login_environment"] is True
    assert login.use_login_environment and not normal.use_login_environment


@pytest.mark.parametrize("read_access_scope", ["workspace", "full_access"])
def test_run_python_reads_follow_setting(
    tmp_path: Path, read_access_scope: str
) -> None:
    db_path, context = _bootstrap_runtime_db(
        tmp_path,
        read_access_scope=read_access_scope,
        allowed_tool_ids=("bash", "run_python"),
    )
    _grant_workspace_full_access(db_path=db_path, capability="process_exec_local")
    broker_context = load_broker_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="run_python",
        path_access_kind="exec",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
    )

    request = build_validated_python_request(
        context=broker_context,
        args=RunPythonToolArgs(code="print(1)"),
        tool_invocation_id=None,
        tool_request_id="request-python-read",
        requested_at="2026-03-23T00:00:02Z",
        preflight_only=True,
    )

    assert ("/" in request.real_read_roots) is (read_access_scope == "full_access")

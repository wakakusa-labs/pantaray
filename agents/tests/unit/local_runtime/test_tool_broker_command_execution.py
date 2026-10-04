from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import bootstrap_local_tooling_catalog
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    PRIVATE_TEMP_DIRNAME,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    encode_message,
)

from .action_seed import insert_agent_action
from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
    _register_folder_mount,
    _seed_broker_actor_process,
    _stub_process_group_resource_registration,
    _stub_subprocess_exec,
)
from .migrated_db import prepare_test_database


@pytest.mark.asyncio
async def test_execute_broker_tool_forwards_shell_command_to_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    _stub_process_group_resource_registration(monkeypatch)
    captured_argv = _stub_subprocess_exec(
        monkeypatch,
        stdout=b"1 passed\n",
    )

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-bash-pytest",
        args={"command": "pytest tests/unit/test_example.py", "cwd": "."},
    )

    assert outcome.status == "success"
    assert outcome.output["stdout"] == "1 passed\n"
    assert captured_argv == [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        "pytest tests/unit/test_example.py",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("process_write", [False, True])
async def test_bash_sandbox_request_uses_manifest_roots_without_workspace_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_write: bool,
) -> None:
    repo_root = tmp_path / "repo"
    read_blocked_root = tmp_path / "read-blocked"
    repo_root.mkdir()
    read_blocked_root.mkdir()
    db_path = tmp_path / "app-data" / "runtime.db"
    db_path.parent.mkdir()
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    _register_folder_mount(
        db_path=db_path,
        real_path=repo_root,
        display_name="Repo",
    )
    _register_folder_mount(
        db_path=db_path,
        real_path=read_blocked_root,
        display_name="Read Blocked",
    )
    from pantaray_agents.local_runtime.tooling import bootstrap as bootstrap_module
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    monkeypatch.setattr(
        bootstrap_module,
        "_resolve_verified_app_runtime_python",
        lambda: Path(sys.executable).resolve(),
    )
    context = bootstrap_module.ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE workspace_manifest_roots
            SET can_process_read = 0, can_process_write = ?
            WHERE manifest_id = ? AND real_path = ?
            """,
            (int(process_write), context.manifest_id, str(read_blocked_root.resolve())),
        )
    _stub_process_group_resource_registration(monkeypatch)
    captured_payload: dict[str, object] = {}

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
            captured_payload.update(payload)
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
        tool_request_id="request-bash-manifest-roots",
        args={"command": "pwd", "cwd": str(repo_root)},
    )

    assert outcome.status == "success"
    assert "workspace_id" not in captured_payload
    assert captured_payload["action_storage"]["plan_path"] == str(
        context.workspace_path / "plan.md"
    )
    assert str(context.workspace_path.resolve()) in captured_payload["real_read_roots"]
    real_write_roots = [
        Path(root) for root in cast(list[str], captured_payload["real_write_roots"])
    ]
    assert context.workspace_path.resolve() in real_write_roots
    assert "action_temp_dir" not in captured_payload
    assert str(context.action_temp_dir.resolve()) in captured_payload["real_read_roots"]
    assert context.action_temp_dir.resolve() not in real_write_roots
    invocation_temp_roots = [
        root
        for root in real_write_roots
        if root.parent == db_path.resolve().parent / PRIVATE_TEMP_DIRNAME
    ]
    assert len(invocation_temp_roots) == 1
    assert str(repo_root.resolve()) in captured_payload["real_read_roots"]
    assert repo_root.resolve() in real_write_roots
    assert str(read_blocked_root.resolve()) not in captured_payload["real_read_roots"]
    assert (read_blocked_root.resolve() in real_write_roots) is process_write

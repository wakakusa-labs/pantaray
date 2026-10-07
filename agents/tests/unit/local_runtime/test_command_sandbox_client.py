from __future__ import annotations

import asyncio
import sqlite3
import stat
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerExecutionError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    complete_execution_session,
    create_capability_grant,
    upsert_approval_preference,
)

from .action_seed import insert_agent_action
from .broker_test_support import BROKER_ACTOR_PROCESS_ID, _seed_broker_actor_process
from .migrated_db import prepare_test_database


def _bootstrap_runtime_db(tmp_path: Path) -> tuple[Path, object]:
    db_path = tmp_path / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    executable_path = context.workspace_path / "pwd"
    executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable_path.chmod(
        executable_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return db_path, context


def _grant_workspace_command_access(
    *, db_path: Path, manifest_id: str | None = None
) -> None:
    del manifest_id
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-global-command",
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("workspace_edit_and_command",),
            created_at="2026-03-23T00:00:00Z",
            updated_at="2026-03-23T00:00:00Z",
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=1_000,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-global-process_exec_local",
            user_id="user-1",
            preference_id="pref-global-command",
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at="2026-03-23T00:00:01Z",
        ),
    )


@pytest.mark.asyncio
async def test_helper_spawn_failure_cleans_temp_dir_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_command_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )

    async def _failing_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _failing_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-spawn-failure",
            args={"command": "pwd"},
        )

    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, resource_path
            FROM tool_runtime_resources
            WHERE resource_kind = 'temp_dir'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert resource_row is not None
    assert resource_row[0] == "cleaned"
    assert resource_row[1] is not None
    assert not Path(str(resource_row[1])).exists()


@pytest.mark.asyncio
async def test_initial_resource_conflict_removes_untracked_temp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_command_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )
    original_register = broker_module.register_path_resource
    attempted_paths: list[Path] = []

    def _register_after_terminal(**kwargs):
        attempted_paths.append(Path(kwargs["resource_path"]))
        complete_execution_session(
            db_path=db_path,
            busy_timeout_ms=1_000,
            execution_session_id=context.execution_session_id,
            status="completed",
            completed_at="2026-03-23T00:00:02Z",
        )
        original_register(**kwargs)

    monkeypatch.setattr(
        broker_module,
        "register_path_resource",
        _register_after_terminal,
    )

    with pytest.raises(BrokerExecutionError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-terminal-resource",
            tool_request_id="request-terminal-resource",
            requested_at="2026-03-23T00:00:01Z",
            args={"command": "pwd"},
        )

    assert len(attempted_paths) == 1
    assert not attempted_paths[0].exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_runtime_resources"
        ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_process_group_registration_failure_cleans_temp_dir_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
    from pantaray_agents.local_runtime.tooling.resources import (
        cleanup as cleanup_module,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_command_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )

    class _FakeStdout:
        async def readline(self) -> bytes:
            return b""

    class _FakeStderr:
        async def read(self) -> bytes:
            return b""

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 4321
        returncode = 0

        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()

        async def wait(self) -> int:
            cleanup_calls.append("wait")
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    def _failing_register_process_group_resource(**kwargs):
        del kwargs
        raise RuntimeError("resource registration failed")

    cleanup_calls: list[object] = []
    monkeypatch.setattr(
        cleanup_module.os,
        "killpg",
        lambda pid, signal_value: cleanup_calls.append((pid, signal_value)),
    )
    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        broker_module,
        "register_process_group_resource",
        _failing_register_process_group_resource,
    )

    with pytest.raises(
        BrokerExecutionError,
        match="failed to register command sandbox cleanup resource",
    ):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-registration-failure",
            args={"command": "pwd"},
        )

    with sqlite3.connect(db_path) as connection:
        resource_row = connection.execute(
            """
            SELECT status, resource_path
            FROM tool_runtime_resources
            WHERE resource_kind = 'temp_dir'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    assert resource_row is not None
    assert resource_row[0] == "cleaned"
    assert resource_row[1] is not None
    assert not Path(str(resource_row[1])).exists()
    assert cleanup_calls == [(4321, cleanup_module.signal.SIGKILL), "wait"]


@pytest.mark.asyncio
async def test_user_stop_kills_the_command_and_keeps_its_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止で打ち切られた command は kill され、直前までの出力が終端行に残る。

    停止は cancel として届くので、ネットワーク方針や dependency proxy の違いに
    関係なく、この 1 本の cancel 経路がすべての command 実行に効く。
    """

    import json

    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
    from pantaray_agents.local_runtime.tooling.resources import (
        cleanup as cleanup_module,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_command_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )
    partial_line = (
        json.dumps(
            {
                "request_id": "request-1",
                "stream": "stdout",
                "seq": 1,
                "data": "half done\n",
            }
        )
        + "\n"
    ).encode("utf-8")
    first_chunk_read = asyncio.Event()

    class _FakeStdout:
        def __init__(self) -> None:
            self._sent = False

        async def readline(self) -> bytes:
            if not self._sent:
                self._sent = True
                return partial_line
            first_chunk_read.set()
            await asyncio.sleep(60)
            raise AssertionError("the killed command must not complete")

    class _FakeStderr:
        async def read(self) -> bytes:
            await asyncio.sleep(60)
            return b""

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            del data

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 5150
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()

        async def wait(self) -> int:
            # SIGKILL されたプロセスの wait status。
            self.returncode = -int(cleanup_module.signal.SIGKILL)
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _FakeProcess()

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cleanup_module.os,
        "killpg",
        lambda pid, signal_value: killed.append((pid, signal_value)),
    )
    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    task = asyncio.ensure_future(
        execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-user-stop",
            requested_at="2026-03-23T00:00:01Z",
            args={"command": "pwd"},
        )
    )
    await asyncio.wait_for(first_chunk_read.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed == [(5150, cleanup_module.signal.SIGKILL)]
    with sqlite3.connect(db_path) as connection:
        audit_row = connection.execute(
            "SELECT terminal_outcome, signal, stdout_bytes "
            "FROM command_invocation_audits"
        ).fetchone()
        invocation_row = connection.execute(
            "SELECT status FROM tool_invocations WHERE invocation_id = ?",
            ("bash:request-bash-user-stop",),
        ).fetchone()
        output_row = connection.execute(
            "SELECT output_json FROM tool_outputs WHERE invocation_id = ?",
            ("bash:request-bash-user-stop",),
        ).fetchone()

    assert audit_row == ("canceled", int(cleanup_module.signal.SIGKILL), 10)
    assert invocation_row == ("canceled",)
    assert output_row is not None
    output = json.loads(str(output_row[0]))
    assert output["stdout"] == "half done\n"
    assert output["error"]["error_type"] == "ToolCallCanceled"


class _StubStdin:
    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _StubStdout:
    """停止が届くまで返らない stdout。"""

    def __init__(self, reading: asyncio.Event) -> None:
        self._reading = reading

    async def readline(self) -> bytes:
        self._reading.set()
        await asyncio.sleep(60)
        raise AssertionError("the canceled read must not return")


class _StubStderr:
    async def read(self) -> bytes:
        await asyncio.sleep(60)
        return b""


@pytest.mark.asyncio
async def test_stop_after_the_helper_exited_records_no_termination_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ヘルパーが先に終了していた停止は、SIGKILL を送っていないので記録もしない。"""

    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
    from pantaray_agents.local_runtime.tooling.resources import (
        cleanup as cleanup_module,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_command_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )
    reading = asyncio.Event()

    class _ExitedProcess:
        pid = 5151
        # ヘルパーは停止が届く前に終了済み。
        returncode = 0

        def __init__(self) -> None:
            self.stdin = _StubStdin()
            self.stdout = _StubStdout(reading)
            self.stderr = _StubStderr()

        async def wait(self) -> int:
            return 0

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _ExitedProcess()

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cleanup_module.os,
        "killpg",
        lambda pid, signal_value: killed.append((pid, signal_value)),
    )
    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    task = asyncio.ensure_future(
        execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-exited-stop",
            requested_at="2026-03-23T00:00:01Z",
            args={"command": "pwd"},
        )
    )
    await asyncio.wait_for(reading.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed == []
    with sqlite3.connect(db_path) as connection:
        audit_row = connection.execute(
            "SELECT terminal_outcome, signal FROM command_invocation_audits"
        ).fetchone()
    assert audit_row == ("canceled", None)


@pytest.mark.asyncio
async def test_stop_racing_a_self_exit_records_the_real_wait_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """killpg がグループを見つけられなかった停止は、SIGKILL 終了として記録しない。

    子プロセスの reaping と ``returncode`` の反映のあいだに停止が届くと、
    ``returncode is None`` のまま ``killpg`` が ``ProcessLookupError`` になる。
    その command は自分で終了しており、送れなかった SIGKILL を載せてはいけない。
    """

    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
    from pantaray_agents.local_runtime.tooling.resources import (
        cleanup as cleanup_module,
    )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_command_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
    )
    reading = asyncio.Event()

    class _ReapedProcess:
        pid = 5152
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdin = _StubStdin()
            self.stdout = _StubStdout(reading)
            self.stderr = _StubStderr()

        async def wait(self) -> int:
            # 停止が届く直前に exit 0 で終了していた。
            self.returncode = 0
            return self.returncode

    def _already_reaped(pid: int, signal_value: int) -> None:
        del pid, signal_value
        raise ProcessLookupError

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del args, kwargs
        return _ReapedProcess()

    monkeypatch.setattr(cleanup_module.os, "killpg", _already_reaped)
    monkeypatch.setattr(
        broker_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    task = asyncio.ensure_future(
        execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-bash-reaped-stop",
            requested_at="2026-03-23T00:00:01Z",
            args={"command": "pwd"},
        )
    )
    await asyncio.wait_for(reading.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with sqlite3.connect(db_path) as connection:
        audit_row = connection.execute(
            "SELECT terminal_outcome, signal FROM command_invocation_audits"
        ).fetchone()
    assert audit_row == ("canceled", None)

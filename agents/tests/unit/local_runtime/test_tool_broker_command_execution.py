from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import cast

import pytest

from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    build_finalized_tool_step,
    project_tool_step_for_persistence,
)
from pantaray_agents.local_runtime.action_conversation.sqlite_visibility import (
    register_action_tool_visibility_sqlite,
)
from pantaray_agents.local_runtime.action_conversation.tool_authority import (
    ActionToolOutputAuthority,
    load_action_tool_output_authority_in_connection,
)
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    resolve_private_temp_root,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerCompletionPersistenceError,
    BrokerToolOutcome,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.models import ToolInvocationCompletionInput
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.tool_invocation_recovery import (
    cancel_inflight_tool_invocation,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    encode_message,
)
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.schema.agent.base import AgentError

from .action_seed import insert_agent_action
from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
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
        if root.parent == resolve_private_temp_root(db_path=db_path)
    ]
    assert len(invocation_temp_roots) == 1
    assert str(repo_root.resolve()) in captured_payload["real_read_roots"]
    assert repo_root.resolve() in real_write_roots
    assert str(read_blocked_root.resolve()) not in captured_payload["real_read_roots"]
    assert (read_blocked_root.resolve() in real_write_roots) is process_write


def _cancel_from_outside(db_path: Path, invocation_id: str) -> None:
    """What a Stop or a superseding user message does while the command runs."""

    result = cancel_inflight_tool_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id=invocation_id,
        completed_at="2026-03-23T00:00:05Z",
    )
    assert result.canceled is True


def _race_command_with_outside_cancel(
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_path: Path,
    after_cancel: str,
) -> None:
    """Close the invocation from outside the call before the broker writes it."""

    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    run_command = broker_module.run_command_via_sandbox

    async def _run_then_lose_the_race(**kwargs):
        outcome = await run_command(**kwargs)
        _cancel_from_outside(db_path, kwargs["request"].tool_invocation_id)
        if after_cancel == "raise":
            raise RuntimeError("the command's own failure after the cancel")
        if after_cancel == "cancelled":
            raise asyncio.CancelledError
        return outcome

    monkeypatch.setattr(
        broker_module, "run_command_via_sandbox", _run_then_lose_the_race
    )


def _insert_processing_step(db_path: Path, *, step_id: str) -> None:
    """The step row the Action emits before the call, which its invocation joins."""

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO agent_action_steps(
                   step_id,action_id,user_id,step_number,step_type,step_name,
                   status,started_at,created_at
               ) VALUES (?,'action-1','user-1',1,'tool_execution','tool::bash',
                         'processing',?,?)""",
            (step_id, "2026-03-23T00:00:01Z", "2026-03-23T00:00:01Z"),
        )


def _save_step_and_read_it_back(
    db_path: Path, *, invocation_id: str, output: FinalizedToolOutput
) -> ActionToolOutputAuthority | None:
    """Persist the error step the Action writes for this call, then authorize it."""

    projection = project_tool_step_for_persistence(
        build_finalized_tool_step(
            status="error",
            output=output,
            error=AgentError(
                error_type="tool_execution_error",
                error_code="ACTION_TOOL_BASH_FAILED",
                error_message="canceled before completion",
                severity="warning",
            ),
        )
    )
    with sqlite3.connect(db_path) as connection:
        register_action_tool_visibility_sqlite(connection)
        (step_id,) = connection.execute(
            "SELECT step_id FROM tool_invocations WHERE invocation_id = ?",
            (invocation_id,),
        ).fetchone()
        connection.execute(
            """UPDATE agent_action_steps
               SET status = 'error', tool_output = ?, completed_at = ?
               WHERE step_id = ?""",
            (json.dumps(projection.output_json), "2026-03-23T00:00:06Z", step_id),
        )
        return load_action_tool_output_authority_in_connection(
            connection=connection,
            user_id="user-1",
            action_id="action-1",
            step_id=step_id,
        )


def _invocation_terminal(db_path: Path, invocation_id: str) -> tuple[str, str]:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT invocation.status, output.output_json
            FROM tool_invocations AS invocation
            JOIN tool_outputs AS output USING (invocation_id)
            WHERE invocation.invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
    return str(row[0]), str(row[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_cancel", ["return", "raise"])
async def test_command_finishing_after_an_outside_cancel_stays_canceled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_cancel: str,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch, stderr=b"build failed\n", returncode=1)
    _race_command_with_outside_cancel(
        monkeypatch, db_path=db_path, after_cancel=after_cancel
    )
    _insert_processing_step(db_path, step_id="request-bash-race")

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-race",
        tool_request_id="request-bash-race",
        args={"command": "make build", "cwd": "."},
    )

    assert isinstance(outcome, BrokerToolOutcome)
    assert outcome.status == "error"
    assert outcome.failure is not None
    assert outcome.failure.error_type == "ToolCallCanceled"
    assert outcome.tool_invocation_id == "invocation-race"
    status, output_json = _invocation_terminal(db_path, "invocation-race")
    assert status == "canceled"
    assert "ActionCanceledToolInvocation" in output_json
    # The step the caller saves from this outcome reads back as that invocation.
    assert outcome.output == json.loads(output_json)
    authority = _save_step_and_read_it_back(
        db_path,
        invocation_id="invocation-race",
        output=FinalizedToolOutput(
            output=outcome.output,
            storage_kind=outcome.output_storage_kind,
            owner_kind="tool_invocation",
            search_text=outcome.search_text,
            stdout_text=outcome.stdout_text,
            stderr_text=outcome.stderr_text,
        ),
    )
    assert authority is not None
    assert authority.output_owner_id == "invocation-race"


@pytest.mark.asyncio
async def test_cancellation_after_an_outside_cancel_is_not_logged_as_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch)
    _race_command_with_outside_cancel(
        monkeypatch, db_path=db_path, after_cancel="cancelled"
    )

    with pytest.raises(asyncio.CancelledError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-race",
            tool_request_id="request-bash-race",
            args={"command": "make build", "cwd": "."},
        )

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    status, output_json = _invocation_terminal(db_path, "invocation-race")
    assert status == "canceled"
    assert "ActionCanceledToolInvocation" in output_json


@pytest.mark.asyncio
async def test_terminal_conflict_without_a_cancel_is_a_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch)
    run_command = broker_module.run_command_via_sandbox

    async def _run_then_find_it_completed(**kwargs):
        outcome = await run_command(**kwargs)
        record_tool_invocation_completion(
            db_path=db_path,
            busy_timeout_ms=1_000,
            completion=ToolInvocationCompletionInput(
                invocation_id=kwargs["request"].tool_invocation_id,
                status="completed",
                completed_at="2026-03-23T00:00:05Z",
                output_json={"ok": True},
                output_storage_kind="inline_json",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
                redaction_applied=False,
            ),
        )
        return outcome

    monkeypatch.setattr(
        broker_module, "run_command_via_sandbox", _run_then_find_it_completed
    )

    with pytest.raises(BrokerCompletionPersistenceError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-race",
            tool_request_id="request-bash-race",
            args={"command": "make build", "cwd": "."},
        )


@pytest.mark.asyncio
async def test_a_failed_rollback_after_an_outside_cancel_is_a_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from pantaray_agents.local_runtime.tooling import tool_result_finalization

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch, stderr=b"build failed\n", returncode=1)
    _race_command_with_outside_cancel(
        monkeypatch, db_path=db_path, after_cancel="return"
    )

    def _cannot_remove(_stored: object) -> None:
        raise PermissionError("result file cannot be removed")

    monkeypatch.setattr(
        tool_result_finalization, "discard_stored_tool_result", _cannot_remove
    )

    with pytest.raises(BrokerCompletionPersistenceError):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            invocation_id="invocation-race",
            tool_request_id="request-bash-race",
            args={"command": "make build", "cwd": "."},
        )

    assert any(
        record.levelno >= logging.ERROR and "orphaned" in record.getMessage()
        for record in caplog.records
    )

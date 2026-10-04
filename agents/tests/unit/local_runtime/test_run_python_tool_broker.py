from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
    bootstrap_local_tooling_catalog,
    create_capability_grant,
    ensure_action_scratch_execution_context,
    start_local_tool_invocation_audit,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.action_session_temp_paths import (
    PRIVATE_TEMP_DIRNAME,
)
from pantaray_agents.local_runtime.tooling.audit_payloads import (
    build_run_python_request_audit_args,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.models import ToolRuntimeResourceCreateInput
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    create_tool_runtime_resource,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    SandboxOutputChunk,
    encode_message,
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
        allowed_tool_ids=("read", "apply_patch", "bash", "run_python"),
    )
    return db_path, context


def _grant_python_access(*, db_path: Path, manifest_id: str | None = None) -> None:
    del manifest_id
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=1_000,
        preference=ApprovalPreferenceUpsertInput(
            preference_id="pref-global",
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
            preference_id="pref-global",
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at="2026-03-23T00:00:01Z",
        ),
    )


def _stub_process_group_resource_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    def _register_process_group_resource(**kwargs) -> str:
        resource_id = (
            f"resource-{kwargs['tool_invocation_id'] or kwargs['action_id']}-process"
        )
        create_tool_runtime_resource(
            db_path=kwargs["db_path"],
            busy_timeout_ms=kwargs["busy_timeout_ms"],
            resource=ToolRuntimeResourceCreateInput(
                resource_id=resource_id,
                execution_session_id=kwargs["execution_session_id"],
                tool_invocation_id=kwargs["tool_invocation_id"],
                action_id=kwargs["action_id"],
                resource_kind="process_group",
                status="active",
                created_at=kwargs["created_at"],
                pid=kwargs["pid"],
                pgid=kwargs["pid"],
                process_start_signature="fake-start-signature",
                resource_path=None,
            ),
        )
        return resource_id

    monkeypatch.setattr(
        broker_module,
        "register_process_group_resource",
        _register_process_group_resource,
    )


@pytest.mark.asyncio
async def test_run_python_tool_writes_generated_code_to_invocation_temp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_python_access(db_path=db_path, manifest_id=context.manifest_id)
    _stub_process_group_resource_registration(monkeypatch)
    captured_argv: list[str] = []
    captured_script_text: list[str] = []

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
                    SandboxOutputChunk(
                        request_id=request_id,
                        stream="stdout",
                        seq=0,
                        data="3\n",
                    )
                ),
                encode_message(
                    SandboxCommandCompletion(
                        request_id=request_id,
                        outcome="exited",
                        exit_code=0,
                        signal=None,
                        stdout_bytes=len(b"3\n"),
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
            captured_argv[:] = list(payload["argv"])
            captured_script_text.append(Path(captured_argv[1]).read_text())
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
        tool_id="run_python",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        tool_request_id="request-run-python",
        args={"code": "print(1 + 2)", "args": []},
    )

    assert outcome.status == "success"
    assert outcome.output["stdout"] == "3\n"
    assert captured_argv[0] == str(context.app_runtime_python)
    script_temp_root = Path(captured_argv[1]).parent.parent
    assert script_temp_root == db_path.resolve().parent / PRIVATE_TEMP_DIRNAME
    assert not Path(captured_argv[1]).is_relative_to(context.action_temp_dir)
    assert captured_script_text == ["print(1 + 2)"]


def test_run_python_queued_audit_redacts_generated_source(tmp_path: Path) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    source = "print('secret-token')\n"
    audit_args = build_run_python_request_audit_args(
        args={"code": source, "args": ["--demo"], "cwd": "."}
    )

    invocation_id = start_local_tool_invocation_audit(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        step_id="step-run-python",
        tool_id="run_python",
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        args={"code": source, "args": ["--demo"], "cwd": "."},
        tool_request_id="request-run-python-redaction",
        started_at="2026-03-23T00:00:00Z",
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT command_summary_json, capability_snapshot_json
            FROM tool_invocations
            WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()

    assert row is not None
    code_summary = audit_args["code"]
    assert code_summary == {
        "sha256": sha256(source.encode("utf-8")).hexdigest(),
        "size_bytes": len(source.encode("utf-8")),
        "line_count": 1,
    }
    assert "secret-token" not in json.dumps(audit_args)
    assert "secret-token" not in json.dumps(tuple(row))

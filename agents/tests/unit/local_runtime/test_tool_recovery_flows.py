from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from pantaray_agents.local_runtime import list_invocation_recovery_audit_events
from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    bootstrap_local_tooling_catalog,
    ensure_action_scratch_execution_context,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import execute_broker_tool
from pantaray_agents.local_runtime.tooling.models import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
    ToolInvocationCompletionInput,
    ToolInvocationStartInput,
)
from pantaray_agents.local_runtime.tooling.repository import (
    create_capability_grant,
    record_tool_invocation_start,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.repository.executions import (
    record_tool_invocation_completion,
)
from pantaray_agents.local_runtime.tooling.resources.resource_recovery import (
    reconcile_tool_runtime_resources_for_startup,
)
from pantaray_agents.local_runtime.tooling.resources.tool_invocation_recovery import (
    cancel_inflight_tool_invocation,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    encode_message,
)
from pantaray_agents.local_runtime.tooling.sandbox.runtime_policy import (
    BrokerLocalBudget,
    RuntimeBudgetResolution,
    SandboxLaunchBudget,
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


def _grant_workspace_full_access(
    *, db_path: Path, manifest_id: str | None = None, capability: str
) -> None:
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
            grant_id=f"grant-global-{capability}",
            user_id="user-1",
            preference_id="pref-global",
            capability=capability,
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at="2026-03-23T00:00:01Z",
        ),
    )


def _stub_runtime_budget(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout_ms: int,
) -> None:
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.brokering.broker_command_validation.resolve_runtime_budget",
        lambda **_kwargs: RuntimeBudgetResolution(
            broker_local=BrokerLocalBudget(
                child_count_limit=8,
                open_file_lease_limit=8,
            ),
            sandbox_launch=SandboxLaunchBudget(
                timeout_ms=timeout_ms,
                stdout_max_bytes=1_048_576,
                stderr_max_bytes=1_048_576,
                temp_storage_limit_bytes=67_108_864,
            ),
        ),
    )


def test_cancel_flow_converges_tool_invocation_to_canceled_with_cancel_output(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id="invocation-cancel",
            tool_request_id="request-bash-cancel",
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id="bash",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="process_exec_local",
            network_policy="cloud-proxy-only",
            command_summary_json={
                "summary_kind": "bash",
                "command": "run bash",
                "cwd": ".",
                "timeout_ms": 5_000,
            },
            capability_snapshot_json={"required_capabilities": ["process_exec_local"]},
            request_json={"args": {"command": "sleep 30"}},
            status="running",
            started_at="2026-03-23T00:00:01Z",
        ),
    )

    result = cancel_inflight_tool_invocation(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id="invocation-cancel",
        completed_at="2026-03-23T00:00:02Z",
    )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status, completed_at
            FROM tool_invocations
            WHERE invocation_id = 'invocation-cancel'
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            WHERE invocation_id = 'invocation-cancel'
            """
        ).fetchone()

    assert result.canceled is True
    assert invocation_row == ("canceled", "2026-03-23T00:00:02Z")
    assert output_row is not None
    assert "ActionCanceledToolInvocation" in str(output_row[0])


def test_recovery_failure_does_not_overwrite_terminal_tool_output(
    tmp_path: Path,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id="invocation-terminal",
            tool_request_id="request-terminal",
            user_id="user-1",
            action_id="action-1",
            step_id="step-1",
            tool_id="bash",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="process_exec_local",
            network_policy="cloud-proxy-only",
            command_summary_json={
                "summary_kind": "bash",
                "command": "run bash",
                "cwd": ".",
                "timeout_ms": 5_000,
            },
            capability_snapshot_json={"required_capabilities": ["process_exec_local"]},
            request_json={"args": {"command": "pwd"}},
            status="running",
            started_at="2026-03-23T00:00:01Z",
        ),
    )
    record_tool_invocation_completion(
        db_path=db_path,
        busy_timeout_ms=1_000,
        completion=ToolInvocationCompletionInput(
            invocation_id="invocation-terminal",
            status="completed",
            completed_at="2026-03-23T00:00:02Z",
            output_json={"ok": True},
            output_storage_kind="inline_json",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
            redaction_applied=False,
        ),
    )

    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status, completed_at
            FROM tool_invocations
            WHERE invocation_id = 'invocation-terminal'
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            WHERE invocation_id = 'invocation-terminal'
            """
        ).fetchone()

    assert invocation_row == ("completed", "2026-03-23T00:00:02Z")
    assert output_row == ('{"ok": true}',)


@pytest.mark.asyncio
async def test_timeout_flow_converges_tool_invocation_to_failed_with_timeout_output(
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
                        signal=9,
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
        pid = 4321
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
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.cleanup.os.killpg",
        lambda _pid, _sig: None,
    )
    monkeypatch.setattr(
        "pantaray_agents.local_runtime.tooling.resources.resource_recovery_store.classify_process_identity",
        lambda pid, expected_start_signature: "mismatch_or_absent",
    )
    _stub_runtime_budget(monkeypatch, timeout_ms=1)

    outcome = await execute_broker_tool(
        db_path=db_path,
        busy_timeout_ms=1_000,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        invocation_id="invocation-timeout",
        tool_request_id="request-bash-timeout",
        args={"command": "pwd"},
    )

    reconcile_tool_runtime_resources_for_startup(
        db_path=db_path,
        busy_timeout_ms=1_000,
    )

    with sqlite3.connect(db_path) as connection:
        invocation_row = connection.execute(
            """
            SELECT status, completed_at
            FROM tool_invocations
            WHERE invocation_id = 'invocation-timeout'
            """
        ).fetchone()
        output_row = connection.execute(
            """
            SELECT output_json
            FROM tool_outputs
            WHERE invocation_id = 'invocation-timeout'
            """
        ).fetchone()
        resource_rows = connection.execute(
            """
            SELECT resource_kind, status
            FROM tool_runtime_resources
            WHERE tool_invocation_id = 'invocation-timeout'
            ORDER BY resource_kind ASC
            """
        ).fetchall()
    audit_events = list_invocation_recovery_audit_events(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation_id="invocation-timeout",
    )
    assert outcome.status == "error"
    assert invocation_row is not None
    assert invocation_row[0] == "failed"
    assert isinstance(invocation_row[1], str)
    assert output_row is not None
    assert "ToolTimeoutError" in str(output_row[0])
    assert resource_rows == [("process_group", "cleaned"), ("temp_dir", "cleaned")]
    assert [event.event_type for event in audit_events] == [
        "timeout_cleanup_completed",
        "timeout_tool_invocation_warning",
    ]
    assert [event.severity for event in audit_events] == ["info", "warning"]

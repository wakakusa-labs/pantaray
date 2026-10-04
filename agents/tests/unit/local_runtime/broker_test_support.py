from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.storage.migrations import (
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
    ToolInvocationStartInput,
    bootstrap_local_tooling_catalog,
    create_capability_grant,
    ensure_action_scratch_execution_context,
    record_tool_invocation_start,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.models import ToolRuntimeResourceCreateInput
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    create_workspace_folder,
    update_read_access_scope,
)
from pantaray_agents.local_runtime.tooling.resources.resource_repository import (
    create_tool_runtime_resource,
)
from pantaray_agents.local_runtime.tooling.sandbox.command_sandbox_protocol import (
    SandboxCommandCompletion,
    SandboxLifecycleEvent,
    SandboxOutputChunk,
    encode_message,
)
from pantaray_agents.local_runtime.tooling.sandbox.runtime_policy import (
    BrokerLocalBudget,
    RuntimeBudgetResolution,
    SandboxLaunchBudget,
)

from .action_seed import insert_agent_action
from .migrated_db import prepare_test_database

BROKER_ACTOR_PROCESS_ID = "process:action-1"
BROKER_ACTOR_TIMESTAMP = "2026-03-23T00:00:00Z"


def _seed_broker_actor_process(
    db_path: Path,
    *,
    process_id: str = BROKER_ACTOR_PROCESS_ID,
    action_id: str = "action-1",
    kind: str = "action",
    status: str = "running",
    parent_process_id: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO processes(
                    process_id, user_id, kind, status, action_id, started_at,
                    updated_at, heartbeat_at, next_event_seq, parent_process_id
                ) VALUES (?, 'user-1', ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    process_id,
                    kind,
                    status,
                    action_id,
                    BROKER_ACTOR_TIMESTAMP,
                    BROKER_ACTOR_TIMESTAMP,
                    BROKER_ACTOR_TIMESTAMP,
                    parent_process_id,
                ),
            )


BROKER_ALLOWED_TOOL_IDS = ("read", "list", "glob", "grep", "apply_patch", "bash")


def _bootstrap_runtime_db(
    tmp_path: Path,
    *,
    read_access_scope: str = "workspace",
    allowed_tool_ids: tuple[str, ...] = BROKER_ALLOWED_TOOL_IDS,
) -> tuple[Path, object]:
    # App storage lives apart from the files a test reads as the user's own.
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
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        read_access_scope=read_access_scope,
        now="2026-03-23T00:00:00Z",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=allowed_tool_ids,
    )
    return db_path, context


def _bootstrap_runtime_db_with_registered_folder(
    tmp_path: Path, *, allowed_tool_ids: tuple[str, ...] = BROKER_ALLOWED_TOOL_IDS
):
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    db_path = app_data / "runtime.db"
    prepare_test_database(
        db_path=db_path,
        busy_timeout_ms=1_000,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=1_000)
    insert_agent_action(db_path=db_path)
    _seed_broker_actor_process(db_path)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        read_access_scope="workspace",
        now="2026-03-23T00:00:00Z",
    )
    repo = tmp_path / "repo-a"
    repo.mkdir()
    folder = _register_folder_mount(
        db_path=db_path,
        real_path=repo,
        display_name="Repo A",
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        action_id="action-1",
        started_at="2026-03-23T00:00:00Z",
        allowed_tool_ids=allowed_tool_ids,
    )
    return db_path, context, repo, folder


def _register_folder_mount(
    *,
    db_path: Path,
    real_path: Path,
    display_name: str,
    now: str = "2026-03-23T00:00:00Z",
):
    return create_workspace_folder(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        real_path=real_path,
        display_name=display_name,
        organization_ids=(),
        project_ids=(),
        now=now,
    )


def _grant_workspace_full_access(
    *,
    db_path: Path,
    manifest_id: str | None = None,
    capability: str,
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


def _stub_subprocess_exec(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> list[str]:
    from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module

    captured_argv: list[str] = []
    captured_stdout = stdout.decode("utf-8")
    captured_stderr = stderr.decode("utf-8")

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
                )
            ]
            if captured_stdout:
                self._messages.append(
                    encode_message(
                        SandboxOutputChunk(
                            request_id=request_id,
                            stream="stdout",
                            seq=0,
                            data=captured_stdout,
                        )
                    )
                )
            if captured_stderr:
                self._messages.append(
                    encode_message(
                        SandboxOutputChunk(
                            request_id=request_id,
                            stream="stderr",
                            seq=0,
                            data=captured_stderr,
                        )
                    )
                )
            self._messages.append(
                encode_message(
                    SandboxCommandCompletion(
                        request_id=request_id,
                        outcome="exited",
                        exit_code=returncode,
                        signal=None,
                        stdout_bytes=len(stdout),
                        stderr_bytes=len(stderr),
                    )
                )
            )

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
            request = json.loads(data.decode("utf-8"))
            captured_argv[:] = list(request["argv"])
            self._stdout_pipe.prime(request_id=request["request_id"])

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeProcess:
        pid = 123

        def __init__(self) -> None:
            self.returncode = returncode
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
    return captured_argv


def _record_apply_patch_invocation(
    *,
    db_path: Path,
    context: object,
    invocation_id: str,
    tool_request_id: str,
    started_at: str,
) -> None:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=tool_request_id,
            user_id="user-1",
            action_id="action-1",
            step_id=f"step-{invocation_id}",
            tool_id="apply_patch",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=5_000,
            intent_class="surgical_edit",
            network_policy="cloud-proxy-only",
            command_summary_json={"summary_kind": "apply_patch", "target_paths": []},
            capability_snapshot_json={"required_capabilities": ["scoped_write"]},
            request_json={},
            status="running",
            started_at=started_at,
        ),
    )


def _record_bash_invocation(
    *,
    db_path: Path,
    context: object,
    invocation_id: str,
    tool_request_id: str,
    started_at: str,
    command_summary: str = "pwd",
) -> None:
    record_tool_invocation_start(
        db_path=db_path,
        busy_timeout_ms=1_000,
        invocation=ToolInvocationStartInput(
            invocation_id=invocation_id,
            tool_request_id=tool_request_id,
            user_id="user-1",
            action_id="action-1",
            step_id=f"step-{invocation_id}",
            tool_id="bash",
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            cwd=".",
            timeout_ms=60_000,
            intent_class="process_exec_local",
            network_policy="deny",
            command_summary_json={
                "summary_kind": "bash",
                "command": command_summary,
                "cwd": ".",
                "timeout_ms": 60_000,
            },
            capability_snapshot_json={"required_capabilities": ["process_exec_local"]},
            request_json={},
            status="queued",
            started_at=started_at,
        ),
    )

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.unit.local_runtime.action_seed import insert_agent_action
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _seed_broker_actor_process,
)

from pantaray_agents.local_runtime.app_runtime_verification import (
    LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV,
)
from pantaray_agents.local_runtime.storage.migrations import (
    apply_migrations,
    load_default_migrations,
)
from pantaray_agents.local_runtime.tooling import (
    ApprovalPreferenceUpsertInput,
    CapabilityGrantCreateInput,
    bootstrap_local_tooling_catalog,
    create_capability_grant,
    ensure_action_scratch_execution_context,
    upsert_approval_preference,
)
from pantaray_agents.local_runtime.tooling.bootstrap import (
    _resolve_verified_app_runtime_python,
)
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerToolOutcome,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.models import (
    ActionExecutionContext,
)
from pantaray_agents.local_runtime.tooling.repository.workspace_settings import (
    update_read_access_scope,
)
from pantaray_agents.schema.read_access import ReadAccessScope

CC_PATH = Path("/usr/bin/cc")
SANDBOX_EXEC_PATH = Path("/usr/bin/sandbox-exec")
ONE_SECOND_MS = 1_000
SEATBELT_SKIP_REASON = "requires Darwin with /usr/bin/sandbox-exec and /usr/bin/cc"
INTEGRATION_APPROVAL_TIMESTAMP = "2026-03-26T00:00:00Z"


def seatbelt_available() -> bool:
    return (
        sys.platform == "darwin" and SANDBOX_EXEC_PATH.is_file() and CC_PATH.is_file()
    )


@dataclass(frozen=True, slots=True)
class RuntimeIntegrationTestbed:
    db_path: Path
    context: ActionExecutionContext


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def configure_app_runtime_manifest_env(
    *, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "app-runtime-manifest.json"
    python_path = Path(sys.executable).resolve()
    manifest_path.write_text(
        json.dumps(
            {
                "python_path": str(python_path),
                "python_version": platform.python_version(),
                "python_sha256": _sha256(python_path),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(LOCAL_APP_RUNTIME_MANIFEST_PATH_ENV, str(manifest_path))
    _resolve_verified_app_runtime_python.cache_clear()


def bootstrap_runtime_testbed(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_access_scope: ReadAccessScope = "workspace",
) -> RuntimeIntegrationTestbed:
    configure_app_runtime_manifest_env(monkeypatch=monkeypatch, tmp_path=tmp_path)
    db_path = tmp_path / "runtime.db"
    apply_migrations(
        db_path=db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        migrations=load_default_migrations(),
    )
    bootstrap_local_tooling_catalog(db_path=db_path, busy_timeout_ms=ONE_SECOND_MS)
    action_id = f"action-{uuid.uuid4()}"
    insert_agent_action(
        db_path=db_path,
        action_id=action_id,
        suggestion_id=f"suggestion:{action_id}",
        created_at=INTEGRATION_APPROVAL_TIMESTAMP,
    )
    _seed_broker_actor_process(db_path, action_id=action_id)
    update_read_access_scope(
        db_path=db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        user_id="user-1",
        read_access_scope=read_access_scope,
        now=INTEGRATION_APPROVAL_TIMESTAMP,
    )
    context = ensure_action_scratch_execution_context(
        db_path=db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        user_id="user-1",
        action_id=action_id,
        started_at=INTEGRATION_APPROVAL_TIMESTAMP,
        allowed_tool_ids=("read", "apply_patch", "bash"),
    )
    _grant_global_command_access(db_path=db_path)
    return RuntimeIntegrationTestbed(db_path=db_path, context=context)


def _grant_global_command_access(*, db_path: Path) -> None:
    preference_id = "pref-global-workspace-edit-and-command"
    upsert_approval_preference(
        db_path=db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        preference=ApprovalPreferenceUpsertInput(
            preference_id=preference_id,
            user_id="user-1",
            scope_type="global",
            scope_ref=None,
            approval_mode="always_allow",
            applies_to=("workspace_edit_and_command",),
            created_at=INTEGRATION_APPROVAL_TIMESTAMP,
            updated_at=INTEGRATION_APPROVAL_TIMESTAMP,
        ),
    )
    create_capability_grant(
        db_path=db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        grant=CapabilityGrantCreateInput(
            grant_id="grant-global-process_exec_local",
            user_id="user-1",
            preference_id=preference_id,
            capability="process_exec_local",
            scope_type="global",
            scope_ref=None,
            grant_source="settings",
            granted_at=INTEGRATION_APPROVAL_TIMESTAMP,
        ),
    )


def compile_workspace_binary(
    *,
    workspace_path: Path,
    executable_name: str,
    source_code: str,
) -> Path:
    source_path = workspace_path / f"{executable_name}.c"
    binary_dir = workspace_path / ".venv" / "bin"
    binary_path = binary_dir / executable_name
    binary_dir.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_code, encoding="utf-8")
    subprocess.run(
        [str(CC_PATH), "-O2", "-o", str(binary_path), str(source_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    os.chmod(binary_path, 0o755)
    return binary_path


async def execute_bash(
    *,
    testbed: RuntimeIntegrationTestbed,
    command: str,
    cwd: str | None = None,
    use_login_environment: bool = False,
) -> BrokerToolOutcome:
    return await execute_broker_tool(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=testbed.context.manifest_id,
        execution_session_id=testbed.context.execution_session_id,
        tool_request_id=f"request-{uuid.uuid4()}",
        args={
            "command": command,
            **({"cwd": cwd} if cwd is not None else {}),
            "use_login_environment": use_login_environment,
            **(
                {"justification": "Use your signed-in accounts for this check."}
                if use_login_environment
                else {}
            ),
        },
    )


def load_latest_audit(*, db_path: Path) -> dict[str, Any]:
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT *
            FROM command_invocation_audits
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return dict(row)


def load_latest_temp_resource(*, db_path: Path) -> dict[str, Any]:
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT *
            FROM tool_runtime_resources
            WHERE resource_kind = 'temp_dir'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    return dict(row)


@dataclass(frozen=True, slots=True)
class TcpProbeServer:
    host: str
    port: int
    hits: list[bytes]
    _thread: threading.Thread
    _socket: socket.socket

    def close(self) -> None:
        self._socket.close()
        self._thread.join(timeout=5)


def start_tcp_probe_server() -> TcpProbeServer:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    hits: list[bytes] = []

    def _serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with closing(conn):
            try:
                hits.append(conn.recv(64))
            except OSError:
                return

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    host, port = server.getsockname()
    return TcpProbeServer(
        host=str(host),
        port=int(port),
        hits=hits,
        _thread=thread,
        _socket=server,
    )

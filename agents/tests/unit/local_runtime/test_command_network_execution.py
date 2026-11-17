from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerExecutionError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
    _stub_process_group_resource_registration,
    _stub_subprocess_exec,
)


@pytest.mark.parametrize("tool_id", ["bash", "run_python"])
@pytest.mark.asyncio
async def test_each_command_uses_its_users_current_network_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_id: str
) -> None:
    db_path, context = _bootstrap_runtime_db(
        tmp_path, allowed_tool_ids=("bash", "run_python")
    )
    _stub_process_group_resource_registration(monkeypatch)
    _stub_subprocess_exec(monkeypatch)
    update_command_network_enabled(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-2",
        command_network_enabled=False,
        now="2026-09-11T00:00:00Z",
    )
    for index, enabled in enumerate((None, False, True)):
        if enabled is not None:
            update_command_network_enabled(
                db_path=db_path,
                busy_timeout_ms=1_000,
                user_id="user-1",
                command_network_enabled=enabled,
                now="2026-09-11T00:00:00Z",
            )
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id=tool_id,
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id=f"network-{index}",
            args={"command": "pwd"} if tool_id == "bash" else {"code": "print(1)"},
        )
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT network_policy FROM tool_invocations ORDER BY tool_request_id"
        ).fetchall()
    assert rows == [("allow",), ("deny",), ("allow",)]


@pytest.mark.parametrize("failure", ["storage", "endpoint"])
@pytest.mark.asyncio
async def test_missing_network_authority_prevents_spawn_and_cleans_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    captured_argv = _stub_subprocess_exec(monkeypatch)
    if failure == "storage":
        with sqlite3.connect(db_path) as connection:
            connection.execute("DROP TABLE command_network_preferences")
        error = sqlite3.OperationalError
    else:
        monkeypatch.delenv("PANTARAY_LOCAL_BACKEND_BOUND_PORT")
        error = BrokerExecutionError
    with pytest.raises(error):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=1_000,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="missing-network-authority",
            args={"command": "pwd"},
        )
    assert captured_argv == []
    with sqlite3.connect(db_path) as connection:
        resources = connection.execute(
            "SELECT status, resource_path FROM tool_runtime_resources WHERE tool_invocation_id IS NOT NULL"
        ).fetchall()
    if failure == "storage":
        assert resources == []
    else:
        assert len(resources) == 1
        assert resources[0][0] == "cleaned"
        assert not Path(resources[0][1]).exists()

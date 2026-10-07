from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering import broker as broker_module
from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerPolicyError,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_common import BrokerContext
from pantaray_agents.local_runtime.tooling.brokering.broker_outcome import (
    UnprojectedBrokerToolOutcome,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    ValidatedCommandRequest,
)

from .broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    BROKER_ACTOR_TIMESTAMP,
    _bootstrap_runtime_db,
    _grant_workspace_full_access,
)

BUSY_TIMEOUT_MS = 1_000


def _seed_children_and_claim(
    *, db_path: Path, manifest_id: str, claimed_path: Path
) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            for child_id in ("child-owned", "child-unclaimed"):
                connection.execute(
                    """
                    INSERT INTO processes(
                        process_id, user_id, kind, status, action_id, started_at,
                        updated_at, heartbeat_at, next_event_seq, parent_process_id
                    ) VALUES (?, 'user-1', 'action_subagent', 'running', 'action-1',
                              ?, ?, ?, 1, ?)
                    """,
                    (
                        child_id,
                        BROKER_ACTOR_TIMESTAMP,
                        BROKER_ACTOR_TIMESTAMP,
                        BROKER_ACTOR_TIMESTAMP,
                        BROKER_ACTOR_PROCESS_ID,
                    ),
                )
            connection.execute(
                """
                INSERT INTO action_subagent_resource_claims(
                    claim_id, user_id, action_id, parent_process_id,
                    child_process_id, resource_kind, root_identity,
                    normalized_key, acquired_at
                ) VALUES (
                    'claim-owned', 'user-1', 'action-1', ?, 'child-owned',
                    'workspace_path', ?, ?, ?
                )
                """,
                (
                    BROKER_ACTOR_PROCESS_ID,
                    manifest_id,
                    str(claimed_path.resolve()),
                    BROKER_ACTOR_TIMESTAMP,
                ),
            )


@pytest.mark.asyncio
async def test_command_sandbox_uses_actor_claim_roots_without_parent_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, context = _bootstrap_runtime_db(tmp_path)
    claimed_path = context.workspace_path / "claimed"
    claimed_path.mkdir()
    _seed_children_and_claim(
        db_path=db_path,
        manifest_id=context.manifest_id,
        claimed_path=claimed_path,
    )
    _grant_workspace_full_access(
        db_path=db_path,
        manifest_id=context.manifest_id,
        capability="process_exec_local",
    )

    captured: dict[str, ValidatedCommandRequest] = {}

    async def _capture_request(
        *,
        context: BrokerContext,
        request: ValidatedCommandRequest,
        **kwargs: object,
    ) -> UnprojectedBrokerToolOutcome:
        del kwargs
        captured[context.actor_process_id] = request
        return UnprojectedBrokerToolOutcome(
            status="success",
            output={"status": "success", "exit_code": 0, "stdout": "", "stderr": ""},
        )

    monkeypatch.setattr(broker_module, "run_command_via_sandbox", _capture_request)
    for child_id in (BROKER_ACTOR_PROCESS_ID, "child-owned", "child-unclaimed"):
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=child_id,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id=f"request-{child_id}",
            args={"command": "pwd", "cwd": "."},
        )

    assert captured[BROKER_ACTOR_PROCESS_ID].real_write_roots == []
    assert captured["child-owned"].real_write_roots == [str(claimed_path.resolve())]
    assert captured["child-unclaimed"].real_write_roots == []


@pytest.mark.asyncio
async def test_claim_acquired_after_validation_prevents_spawn_and_approval_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.local_runtime.storage.migrations.connection import (
        configure_connection,
    )
    from pantaray_agents.local_runtime.storage.transactions import immediate_transaction
    from pantaray_agents.local_runtime.tooling.action_subagent_resource_claims import (
        WorkspacePathResourceClaim,
        acquire_action_subagent_resource_claims_in_connection,
    )
    from pantaray_agents.local_runtime.tooling.brokering.action_subagent_broker_authority import (
        ACTION_SUBAGENT_WRITE_DENIED,
    )

    from .broker_test_support import _seed_broker_actor_process

    db_path, context = _bootstrap_runtime_db(tmp_path)
    _grant_workspace_full_access(db_path=db_path, capability="process_exec_local")
    _seed_broker_actor_process(
        db_path,
        process_id="child-race",
        kind="action_subagent",
        parent_process_id=BROKER_ACTOR_PROCESS_ID,
    )
    original_start = broker_module.claim_broker_execution_start

    def _acquire_then_start(**kwargs: object) -> str | None:
        with sqlite3.connect(db_path) as connection:
            configure_connection(connection, BUSY_TIMEOUT_MS)
            connection.row_factory = sqlite3.Row
            with immediate_transaction(connection):
                acquire_action_subagent_resource_claims_in_connection(
                    connection,
                    user_id="user-1",
                    action_id="action-1",
                    parent_process_id=BROKER_ACTOR_PROCESS_ID,
                    child_process_id="child-race",
                    acquired_at=BROKER_ACTOR_TIMESTAMP,
                    claims=(
                        WorkspacePathResourceClaim(
                            claim_id="claim-race",
                            manifest_id=context.manifest_id,
                            raw_path="claimed",
                            current_cwd=context.workspace_path,
                        ),
                    ),
                )
        return original_start(**kwargs)

    async def _unexpected_spawn(**kwargs: object) -> UnprojectedBrokerToolOutcome:
        pytest.fail("a stale write grant reached the sandbox process boundary")

    monkeypatch.setattr(
        broker_module, "claim_broker_execution_start", _acquire_then_start
    )
    monkeypatch.setattr(broker_module, "run_command_via_sandbox", _unexpected_spawn)
    with pytest.raises(BrokerPolicyError) as exc_info:
        await execute_broker_tool(
            db_path=db_path,
            busy_timeout_ms=BUSY_TIMEOUT_MS,
            tool_id="bash",
            user_id="user-1",
            actor_process_id=BROKER_ACTOR_PROCESS_ID,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            tool_request_id="request-race",
            args={"command": "touch claimed/value"},
        )
    assert exc_info.value.code == ACTION_SUBAGENT_WRITE_DENIED
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM tool_invocations"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM command_workspace_write_grants"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status, claimed_at, tool_invocation_id FROM approval_sessions WHERE tool_request_id = 'request-race'"
        ).fetchone() == ("approved_once", None, None)

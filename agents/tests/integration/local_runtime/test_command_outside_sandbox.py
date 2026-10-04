from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.unit.local_runtime.broker_test_support import BROKER_ACTOR_PROCESS_ID

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    BrokerToolOutcome,
    execute_broker_tool,
)
from pantaray_agents.schema.agent.base import JSONValue

from .support import (
    ONE_SECOND_MS,
    SEATBELT_SKIP_REASON,
    RuntimeIntegrationTestbed,
    bootstrap_runtime_testbed,
    load_latest_audit,
    seatbelt_available,
)
from .test_command_sandbox_outside_workspace import _approve_once

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


async def _bash(
    testbed: RuntimeIntegrationTestbed,
    *,
    command: str,
    request_id: str,
    outside_sandbox: bool,
) -> BrokerToolOutcome:
    args: dict[str, JSONValue] = {"command": command}
    if outside_sandbox:
        args["run_outside_sandbox"] = True
        args["justification"] = "Write a test file."
    outcome = await execute_broker_tool(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=testbed.context.manifest_id,
        execution_session_id=testbed.context.execution_session_id,
        invocation_id=f"invocation-{request_id}",
        tool_request_id=request_id,
        args=args,
    )
    assert isinstance(outcome, BrokerToolOutcome)
    return outcome


@pytest.mark.asyncio
async def test_an_approved_run_reaches_what_the_sandbox_denies(
    tmp_path: Path, outside_temp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    testbed = bootstrap_runtime_testbed(tmp_path=app_data, monkeypatch=monkeypatch)
    # A private storage file to read and a scratch folder outside the workspace
    # to write: the sandbox denies both.
    private_probe = app_data / "probe.txt"
    private_probe.write_text("private\n", encoding="utf-8")
    scratch = outside_temp_path / "made.txt"
    command = (
        f"cat {private_probe} || echo read-denied; "
        f"echo made > {scratch} || echo write-denied"
    )

    sandboxed = await _bash(
        testbed, command=command, request_id="request-sandboxed", outside_sandbox=False
    )
    assert sandboxed.output["stdout"] == "read-denied\nwrite-denied\n"
    assert not scratch.exists()

    # The testbed auto-approves commands; a run outside the sandbox still asks.
    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _bash(
            testbed, command=command, request_id="request-outside", outside_sandbox=True
        )
    _approve_once(testbed, asked.value, "request-outside")
    outcome = await _bash(
        testbed, command=command, request_id="request-outside", outside_sandbox=True
    )

    assert outcome.status == "success", outcome.output
    assert outcome.output["stdout"] == "private\n"
    assert scratch.read_text(encoding="utf-8") == "made\n"

    # The audit names the approval and records the unsandboxed summary.
    audit = load_latest_audit(db_path=testbed.db_path)
    assert audit["invocation_id"] == "invocation-request-outside"
    assert audit["approval_session_id"] == asked.value.approval_session_id
    assert audit["terminal_outcome"] == "exited"
    with sqlite3.connect(testbed.db_path) as connection:
        invocation_summary, approval_status, approval_source = connection.execute(
            """
            SELECT invocation.command_summary_json, approval.status,
                   approval.approval_source
            FROM tool_invocations AS invocation
            JOIN approval_sessions AS approval
              ON approval.approval_session_id = ?
            WHERE invocation.invocation_id = 'invocation-request-outside'
            """,
            (asked.value.approval_session_id,),
        ).fetchone()
    assert json.loads(invocation_summary)["run_outside_sandbox"] is True
    assert (approval_status, approval_source) == ("approved_once", "prompt")

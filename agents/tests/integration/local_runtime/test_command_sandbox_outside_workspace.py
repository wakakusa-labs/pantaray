from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.local_runtime.broker_test_support import BROKER_ACTOR_PROCESS_ID

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    BrokerToolOutcome,
    apply_approval_decision,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.repository import load_execution_session
from pantaray_agents.schema.agent.base import JSONValue

from .support import (
    INTEGRATION_APPROVAL_TIMESTAMP,
    ONE_SECOND_MS,
    SEATBELT_SKIP_REASON,
    RuntimeIntegrationTestbed,
    bootstrap_runtime_testbed,
    seatbelt_available,
)

pytestmark = pytest.mark.skipif(not seatbelt_available(), reason=SEATBELT_SKIP_REASON)


async def _bash(
    testbed: RuntimeIntegrationTestbed,
    *,
    command: str,
    cwd: Path | None = None,
    write_folders: tuple[Path, ...] = (),
    request_id: str = "request-outside",
) -> BrokerToolOutcome:
    args: dict[str, JSONValue] = {"command": command}
    if cwd is not None:
        args["cwd"] = str(cwd)
    if write_folders:
        args["additional_write_folders"] = [str(folder) for folder in write_folders]
        args["justification"] = "Save the tool's settings."
    outcome = await execute_broker_tool(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        tool_id="bash",
        user_id="user-1",
        actor_process_id=BROKER_ACTOR_PROCESS_ID,
        manifest_id=testbed.context.manifest_id,
        execution_session_id=testbed.context.execution_session_id,
        tool_request_id=request_id,
        args=args,
    )
    assert isinstance(outcome, BrokerToolOutcome)
    return outcome


def _approve_once(
    testbed: RuntimeIntegrationTestbed,
    asked: BrokerApprovalRequiredError,
    request_id: str = "request-outside",
) -> None:
    session = load_execution_session(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        execution_session_id=testbed.context.execution_session_id,
    )
    assert session.action_id is not None
    apply_approval_decision(
        db_path=testbed.db_path,
        busy_timeout_ms=ONE_SECOND_MS,
        tool_request_id=request_id,
        user_id="user-1",
        action_id=session.action_id,
        approval_session_id=asked.approval_session_id,
        decision="approved_once",
        decided_at=INTEGRATION_APPROVAL_TIMESTAMP,
    )


@pytest.mark.asyncio
async def test_approved_outside_cwd_is_writable_only_inside_that_folder(
    tmp_path: Path, outside_temp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    testbed = bootstrap_runtime_testbed(tmp_path=app_data, monkeypatch=monkeypatch)
    outside = outside_temp_path / "outside"
    outside.mkdir()
    sibling = outside_temp_path / "sibling.txt"
    command = f"echo made > made.txt; echo leak > {sibling} || echo denied"

    # The testbed auto-approves commands; an outside cwd still asks.
    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _bash(testbed, cwd=outside, command=command)
    _approve_once(testbed, asked.value)
    outcome = await _bash(testbed, cwd=outside, command=command)

    assert outcome.status == "success", outcome.output
    assert (outside / "made.txt").read_text() == "made\n"
    assert outcome.output["stdout"] == "denied\n"
    assert not sibling.exists()


@pytest.mark.asyncio
async def test_requested_write_folder_is_writable_only_for_the_approved_call(
    tmp_path: Path, outside_temp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    testbed = bootstrap_runtime_testbed(tmp_path=app_data, monkeypatch=monkeypatch)
    requested = outside_temp_path / "tool-settings"
    requested.mkdir()
    sibling = outside_temp_path / "sibling"
    sibling.mkdir()
    # Runs in the workspace and writes the requested folder and its sibling.
    command = (
        f"echo made > {requested / 'made.txt'}; "
        f"echo leak > {sibling / 'leak.txt'} || echo denied"
    )

    with pytest.raises(BrokerApprovalRequiredError) as asked:
        await _bash(testbed, command=command, write_folders=(requested,))
    _approve_once(testbed, asked.value)
    outcome = await _bash(testbed, command=command, write_folders=(requested,))

    assert outcome.status == "success", outcome.output
    assert (requested / "made.txt").read_text() == "made\n"
    assert outcome.output["stdout"] == "denied\n"
    assert not (sibling / "leak.txt").exists()

    # The approval opened the folder for that call only.
    later = await _bash(
        testbed,
        command=f"echo again > {requested / 'again.txt'} || echo denied",
        request_id="request-later",
    )
    assert later.output["stdout"] == "denied\n"
    assert not (requested / "again.txt").exists()

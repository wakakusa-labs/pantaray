"""Background bash processes driven through the broker on real sandbox helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from pantaray_agents.local_runtime.tooling.brokering.broker import (
    BrokerApprovalRequiredError,
    BrokerToolOutcome,
    FinalizedBrokerPolicyError,
    apply_approval_decision,
    execute_broker_tool,
)
from pantaray_agents.local_runtime.tooling.brokering.broker_protocol import (
    BashToolArgs,
)
from pantaray_agents.local_runtime.tooling.repository.command_network_settings import (
    update_command_network_enabled,
)
from pantaray_agents.local_runtime.tooling.resources.resource_cleanup import (
    process_exists,
)
from pantaray_agents.local_runtime.tooling.sandbox import runtime_policy
from pantaray_agents.local_runtime.tooling.sandbox.process_supervisor import (
    stop_owner_processes,
)
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.tools.contract import BrokerPolicyError

from .broker_test_support import BROKER_ACTOR_PROCESS_ID, _bootstrap_runtime_db

# Bounds every broker call; none should take more than a few seconds.
WAIT_TIMEOUT_S = 20
TOOL_IDS = ("bash", "read_process_output", "stop_process")
# A background start always runs under seatbelt: run_outside_sandbox is refused.
needs_seatbelt = pytest.mark.skipif(
    not Path("/usr/bin/sandbox-exec").is_file(), reason="needs macOS seatbelt"
)


@dataclass(frozen=True, slots=True)
class _Action:
    db_path: Path
    manifest_id: str
    execution_session_id: str
    workspace: Path

    async def call(
        self,
        tool_id: str,
        args: dict[str, JSONValue],
        *,
        request_id: str,
        preflight_only: bool = False,
    ) -> dict[str, JSONValue]:
        outcome = await asyncio.wait_for(
            execute_broker_tool(
                db_path=self.db_path,
                busy_timeout_ms=1_000,
                tool_id=tool_id,
                user_id="user-1",
                actor_process_id=BROKER_ACTOR_PROCESS_ID,
                manifest_id=self.manifest_id,
                execution_session_id=self.execution_session_id,
                args=args,
                tool_request_id=request_id,
                requested_at="2026-03-23T00:00:02Z",
                preflight_only=preflight_only,
            ),
            WAIT_TIMEOUT_S,
        )
        if preflight_only:
            return {}
        assert isinstance(outcome, BrokerToolOutcome)
        assert outcome.status == "success", outcome.output
        assert isinstance(outcome.output, dict)
        return outcome.output

    async def refused(
        self, tool_id: str, args: dict[str, JSONValue], *, request_id: str
    ) -> FinalizedBrokerPolicyError:
        with pytest.raises(FinalizedBrokerPolicyError) as refusal:
            await self.call(tool_id, args, request_id=request_id)
        return refusal.value


@pytest.fixture
async def action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Action]:
    monkeypatch.setattr(runtime_policy, "PROCESS_START_WINDOW_MS", 300)
    monkeypatch.setattr(runtime_policy, "PROCESS_STOP_GRACE_MS", 300)
    monkeypatch.setattr(runtime_policy, "PROCESS_OUTPUT_DRAIN_MS", 100)
    monkeypatch.setattr(runtime_policy, "PROCESS_STOP_REPORT_MARGIN_MS", 300)
    # Command temp dirs go under ~/Library/Caches.
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    db_path, context = _bootstrap_runtime_db(tmp_path, allowed_tool_ids=TOOL_IDS)
    update_command_network_enabled(
        db_path=db_path,
        busy_timeout_ms=1_000,
        user_id="user-1",
        command_network_enabled=False,
        now="2026-03-23T00:00:01Z",
    )
    workspace = context.workspace_path
    try:
        yield _Action(
            db_path=db_path,
            manifest_id=context.manifest_id,
            execution_session_id=context.execution_session_id,
            workspace=workspace,
        )
    finally:
        await asyncio.wait_for(
            stop_owner_processes(owner_process_id=BROKER_ACTOR_PROCESS_ID),
            WAIT_TIMEOUT_S,
        )


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(WAIT_TIMEOUT_S):
        while not condition():
            await asyncio.sleep(0.05)


# Catches: the process tools not routed to the supervisor or not registered,
# the owner not being the calling run, results outside the tools' output
# schema, a stop that leaves the command running, and refusals the model
# cannot act on because they do not name the running processes.
@needs_seatbelt
@pytest.mark.asyncio
async def test_a_background_command_is_started_read_and_stopped(
    action: _Action, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_policy, "PROCESS_RUN_LIMIT", 1)
    started = await action.call(
        "bash",
        {
            # Says it is listening only after the start window has passed.
            "command": "echo $$ > server.pid; sleep 0.5; echo listening; "
            "while :; do sleep 0.05; done",
            "run_in_background": True,
        },
        request_id="start",
    )
    assert started["status"] == "running"
    assert started["process_id"] == "p1"
    assert "listening" not in str(started["stdout"])
    pid_file = action.workspace / "server.pid"
    await _until(lambda: pid_file.exists() and bool(pid_file.read_text().strip()))
    command_pid = int(pid_file.read_text())

    seen = str(started["stdout"])
    reads = 0
    while "listening" not in seen:
        reads += 1
        read = await action.call(
            "read_process_output",
            {"process_id": "p1", "wait_seconds": 0.1},
            request_id=f"read-{reads}",
        )
        assert read["status"] == "running"
        seen += str(read["stdout"])

    over_limit = await action.refused(
        "bash",
        {"command": "sleep 30", "run_in_background": True},
        request_id="second-start",
    )
    assert over_limit.code == "PROCESS_LIMIT_REACHED"
    assert "p1" in str(over_limit)

    unknown = await action.refused(
        "stop_process", {"process_id": "p9"}, request_id="stop-unknown"
    )
    assert unknown.code == "PROCESS_NOT_FOUND"
    assert "p1" in str(unknown)

    stopped = await action.call("stop_process", {"process_id": "p1"}, request_id="stop")
    assert stopped["status"] == "stopped"
    await _until(lambda: not process_exists(command_pid))
    again = await action.call(
        "stop_process", {"process_id": "p1"}, request_id="stop-again"
    )
    assert again["status"] == "stopped"


# Catches: a command that ends at once being reported as running, or its
# output lost, when it ends within the start window.
@needs_seatbelt
@pytest.mark.asyncio
async def test_a_command_that_ends_in_the_start_window_returns_its_end(
    action: _Action,
) -> None:
    ended = await action.call(
        "bash",
        {"command": "echo port in use >&2; exit 3", "run_in_background": True},
        request_id="start",
    )
    assert ended["status"] == "exited"
    assert ended["exit_code"] == 3
    assert ended["stderr"] == "port in use\n"


# Catches: run_in_background left out of the approval summary, which would let
# the user's approval of a call in the foreground start it left running.
@pytest.mark.asyncio
async def test_a_foreground_approval_does_not_approve_a_background_run(
    action: _Action,
) -> None:
    args: dict[str, JSONValue] = {
        "command": "gh auth status",
        "use_login_environment": True,
        "justification": "Check the pull request with your GitHub account.",
    }
    with pytest.raises(BrokerApprovalRequiredError) as approval:
        await action.call("bash", args, request_id="login", preflight_only=True)
    apply_approval_decision(
        db_path=action.db_path,
        busy_timeout_ms=1_000,
        tool_request_id="login",
        user_id="user-1",
        action_id="action-1",
        approval_session_id=approval.value.approval_session_id,
        decision="approved_once",
        decided_at="2026-03-23T00:00:03Z",
    )
    with pytest.raises(BrokerPolicyError, match="does not match"):
        await action.call(
            "bash", {**args, "run_in_background": True}, request_id="login"
        )


# Catches: a process left running with the user's full permissions.
def test_a_background_run_outside_the_sandbox_is_refused() -> None:
    with pytest.raises(ValueError, match="run_in_background cannot be combined"):
        BashToolArgs.model_validate(
            {
                "command": "npm run dev",
                "run_in_background": True,
                "run_outside_sandbox": True,
                "justification": "Start the dev server.",
            }
        )

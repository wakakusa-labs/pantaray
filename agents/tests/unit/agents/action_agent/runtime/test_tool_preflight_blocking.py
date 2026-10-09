from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from tests.unit.local_runtime.broker_test_support import (
    BROKER_ACTOR_PROCESS_ID,
    _bootstrap_runtime_db,
)

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalDeniedToolControl,
    ApprovalRequiredToolControl,
    CompletedToolControl,
    FailedToolControl,
    ToolCompletionAuditPersistenceError,
    ToolExecutionControl,
    ToolExecutionPreparation,
    ToolFailureIdentity,
    UnprojectedToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tools import run_tool
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import BASH_TOOL
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    ActionStepToolResultOwner,
    FinalizedToolOutput,
    ToolResultFinalizationRequest,
    finalize_local_tool_result,
)


def _state() -> dict[str, object]:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-04-27T00:00:00Z",
        max_steps=10,
        max_tool_steps=10,
        token_budget=None,
    )
    state["manifest_id"] = "manifest-1"
    state["execution_session_id"] = "session-1"
    state["execution_network_policy"] = "default"
    return state


def _runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.services = SimpleNamespace(token_accounting=MagicMock())
    return runtime


def _preparation(
    *,
    status: str,
    output: dict[str, object],
    control: ToolExecutionControl,
    step_id: str = "step-1",
) -> ToolExecutionPreparation:
    return ToolExecutionPreparation(
        result=UnprojectedToolExecutionResult(
            step_id=step_id,
            tool_id="bash",
            status=status,
            started_at="2026-04-27T00:00:01Z",
            completed_at="2026-04-27T00:00:01Z",
            output=output,
        ),
        control=control,
        finalized_output=FinalizedToolOutput(
            output=output,
            storage_kind="inline_json",
            owner_kind="action_step",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
        ),
    )


def test_broker_policy_error_payload_includes_repair_hint() -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.broker_tools import (
        _build_error_preparation,
    )
    from pantaray_agents.tools.contract import BrokerPolicyError

    preparation = _build_error_preparation(
        step_id="step-1",
        tool_def=BASH_TOOL,
        error=BrokerPolicyError(
            "cwd must reference an existing workspace directory",
            code="EXEC_CWD_NOT_FOUND",
            fix_hint="Retry with `.` or an existing directory under workspace roots.",
            examples=('tool=bash args={"command":"pwd","cwd":"."}',),
        ),
    )

    error = preparation.result.output["error"]
    assert error["code"] == "EXEC_CWD_NOT_FOUND"
    assert (
        error["fix_hint"]
        == "Retry with `.` or an existing directory under workspace roots."
    )
    assert error["examples"] == ['tool=bash args={"command":"pwd","cwd":"."}']


@pytest.mark.parametrize(
    ("status", "control"),
    (
        ("error", CompletedToolControl()),
        (
            "success",
            FailedToolControl(
                failure=ToolFailureIdentity(
                    error_type="TestFailure",
                    message="failed",
                )
            ),
        ),
        ("processing", CompletedToolControl()),
    ),
)
def test_tool_preparation_rejects_status_control_mismatch(
    status: str,
    control: ToolExecutionControl,
) -> None:
    with pytest.raises(ValueError, match="status/control mismatch"):
        _preparation(status=status, output={"status": status}, control=control)


@pytest.mark.asyncio
async def test_run_tool_returns_preflight_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_broker_tool_wrapper(*_args, **_kwargs) -> ToolExecutionPreparation:
        return _preparation(
            status="error",
            output={
                "error": {
                    "type": "BrokerPolicyError",
                    "message": "workspace command path argument must be workspace-relative",
                }
            },
            control=FailedToolControl(
                failure=ToolFailureIdentity(
                    error_type="BrokerPolicyError",
                    message="workspace command path argument must be workspace-relative",
                )
            ),
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_broker_tool_wrapper",
        fake_broker_tool_wrapper,
    )
    runtime = _runtime()

    result = await run_tool(
        MagicMock(),
        BASH_TOOL,
        args={"command": "find /tmp -maxdepth 1", "cwd": "."},
        state=_state(),
        sink=CountingSink(),
        runtime=runtime,
        step_number=1,
        actor="goal_worker",
    )

    assert result.status == "error"


@pytest.mark.asyncio
async def test_run_tool_returns_preflight_approval_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_broker_tool_wrapper(*_args, **_kwargs) -> ToolExecutionPreparation:
        return _preparation(
            status="processing",
            output={
                "kind": "approval_required",
                "approval_status": "pending",
                "approval_session_id": "approval-1",
                "tool_request_id": "request-1",
            },
            control=ApprovalRequiredToolControl(
                approval_session_id="approval-1",
                tool_request_id="request-1",
                intent_class="process_exec_local",
                command_summary={},
            ),
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_broker_tool_wrapper",
        fake_broker_tool_wrapper,
    )
    runtime = _runtime()

    result = await run_tool(
        MagicMock(),
        BASH_TOOL,
        args={"command": "ls -R .", "cwd": "."},
        state=_state(),
        sink=CountingSink(),
        runtime=runtime,
        step_number=1,
        actor="goal_worker",
        step_id="step-approval-1",
    )

    assert result.status == "processing"
    assert result.tool_request_id == "action-1:step-approval-1:1"
    assert isinstance(result.finalized_output.output, dict)
    assert result.finalized_output.output["kind"] == "approval_required"


@pytest.mark.asyncio
async def test_run_tool_preflight_denial_stops_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_broker_tool_wrapper(*_args, **_kwargs) -> ToolExecutionPreparation:
        nonlocal calls
        calls += 1
        return _preparation(
            step_id="step-denied-1",
            status="success",
            output={
                "kind": "approval_denied",
                "approval_status": "denied",
                "approval_session_id": "approval-1",
                "tool_request_id": "request-1",
                "tool_id": "bash",
                "executed": False,
            },
            control=ApprovalDeniedToolControl(
                approval_session_id="approval-1",
                tool_request_id="request-1",
            ),
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution.run_broker_tool_wrapper",
        fake_broker_tool_wrapper,
    )
    runtime = _runtime()

    result = await run_tool(
        MagicMock(),
        BASH_TOOL,
        args={"command": "ls -R .", "cwd": "."},
        state=_state(),
        sink=CountingSink(),
        runtime=runtime,
        step_number=1,
        actor="goal_worker",
        step_id="step-denied-1",
    )

    assert calls == 1
    assert result.status == "success"
    assert isinstance(result.finalized_output.output, dict)
    assert result.finalized_output.output["kind"] == "approval_denied"
    assert result.finalized_output.output["executed"] is False


@pytest.mark.asyncio
async def test_broker_wrapper_returns_approval_denied_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
        broker_tools,
    )
    from pantaray_agents.local_runtime.tooling.brokering.broker import (
        BrokerApprovalDeniedError,
    )

    captured_kwargs: dict[str, object] = {}

    async def fake_execute_broker_tool(**kwargs: object) -> None:
        captured_kwargs.update(kwargs)
        raise BrokerApprovalDeniedError(approval_session_id="approval-denied-1")

    monkeypatch.setattr(
        broker_tools,
        "read_local_runtime_db_config",
        lambda: ("/tmp/runtime.db", 1_000),
    )
    monkeypatch.setattr(broker_tools, "execute_broker_tool", fake_execute_broker_tool)
    monkeypatch.setattr(
        broker_tools,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )

    result = await broker_tools.run_broker_tool_wrapper(
        MagicMock(),
        "step-denied-1",
        BASH_TOOL,
        {"command": "pwd", "cwd": "."},
        _state(),
        invocation_id=None,
        tool_request_id="request-denied-1",
        requested_at="2026-04-27T00:00:01Z",
        preflight_only=True,
    )

    assert result.result.status == "success"
    assert captured_kwargs["actor_process_id"] == BROKER_ACTOR_PROCESS_ID
    assert result.result.started_at == "2026-04-27T00:00:01Z"
    assert result.result.output == {
        "kind": "approval_denied",
        "approval_status": "denied",
        "approval_session_id": "approval-denied-1",
        "tool_request_id": "request-denied-1",
        "tool_id": "bash",
        "executed": False,
        "message": (
            "The requested tool call was denied by the user. "
            "Treat this tool call as skipped and continue."
        ),
    }


@pytest.mark.parametrize(
    ("command", "expected_storage_kind"),
    (("pwd", "inline_json"), ("x" * 21_000, "action_file")),
)
@pytest.mark.asyncio
async def test_broker_wrapper_projects_only_the_approval_command_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    expected_storage_kind: str,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
        approval_preparation,
        broker_tools,
    )
    from pantaray_agents.local_runtime.tooling.brokering.broker import (
        BrokerApprovalRequiredError,
    )

    async def fake_execute_broker_tool(**_kwargs):
        raise BrokerApprovalRequiredError(
            "approval required",
            approval_session_id="approval-1",
        )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    monkeypatch.setattr(
        broker_tools,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    monkeypatch.setattr(broker_tools, "execute_broker_tool", fake_execute_broker_tool)
    monkeypatch.setattr(
        broker_tools,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )
    monkeypatch.setattr(
        approval_preparation,
        "load_approval_session_by_request",
        lambda **_kwargs: SimpleNamespace(
            intent_class="process_exec_local",
            command_summary_json={"command": command},
        ),
    )
    state = _state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id

    result = await broker_tools.run_broker_tool_wrapper(
        MagicMock(),
        "step-1",
        BASH_TOOL,
        {"command": command, "cwd": "."},
        state,
        invocation_id=None,
        tool_request_id="request-1",
        requested_at="2026-04-27T00:00:01Z",
        preflight_only=True,
    )

    assert isinstance(result.result.output, dict)
    assert result.result.output["kind"] == "approval_required"
    assert result.result.output["command_summary_storage_kind"] == expected_storage_kind
    projected_summary = result.result.output["command_summary"]
    if expected_storage_kind == "inline_json":
        assert projected_summary == {"command": command}
        return
    assert isinstance(projected_summary, dict)
    stored_path = Path(str(projected_summary["path"]))
    assert json.loads(stored_path.read_text(encoding="utf-8")) == {"command": command}


@pytest.mark.asyncio
async def test_approval_envelope_is_finalized_after_inline_summary_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
        approval_preparation,
        broker_tools,
    )
    from pantaray_agents.local_runtime.tooling.brokering.broker import (
        BrokerApprovalRequiredError,
    )

    command = "x" * 19_950

    async def fake_execute_broker_tool(**_kwargs):
        raise BrokerApprovalRequiredError(
            "approval required",
            approval_session_id="approval-1",
        )

    db_path, context = _bootstrap_runtime_db(tmp_path)
    monkeypatch.setattr(
        broker_tools,
        "read_local_runtime_db_config",
        lambda: (db_path, 1_000),
    )
    monkeypatch.setattr(broker_tools, "execute_broker_tool", fake_execute_broker_tool)
    monkeypatch.setattr(
        broker_tools,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )
    monkeypatch.setattr(
        approval_preparation,
        "load_approval_session_by_request",
        lambda **_kwargs: SimpleNamespace(
            intent_class="process_exec_local",
            command_summary_json={"command": command},
        ),
    )
    state = _state()
    state["manifest_id"] = context.manifest_id
    state["execution_session_id"] = context.execution_session_id

    preparation = await broker_tools.run_broker_tool_wrapper(
        MagicMock(),
        "step-1",
        BASH_TOOL,
        {"command": command, "cwd": "."},
        state,
        invocation_id=None,
        tool_request_id="request-1",
        requested_at="2026-04-27T00:00:01Z",
        preflight_only=True,
    )

    assert isinstance(preparation.result.output, dict)
    assert preparation.result.output["command_summary_storage_kind"] == "inline_json"
    finalized = finalize_local_tool_result(
        db_path=db_path,
        busy_timeout_ms=1_000,
        request=ToolResultFinalizationRequest(
            owner=ActionStepToolResultOwner(
                manifest_id=context.manifest_id,
                action_id="action-1",
                user_id="user-1",
                step_id="step-1",
            ),
            output=preparation.result.output,
        ),
    )

    assert finalized.storage_kind == "action_file"
    assert isinstance(finalized.output, dict)
    stored_path = Path(str(finalized.output["path"]))
    stored_envelope = json.loads(stored_path.read_text(encoding="utf-8"))
    assert stored_envelope["kind"] == "approval_required"
    assert stored_envelope["command_summary"] == {"command": command}


@pytest.mark.asyncio
async def test_broker_wrapper_classifies_completion_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
        broker_tools,
    )
    from pantaray_agents.local_runtime.tooling.brokering.broker import (
        BrokerCompletionPersistenceError,
    )

    async def fake_execute_broker_tool(**_kwargs):
        raise BrokerCompletionPersistenceError(tool_invocation_id="invocation-1")

    monkeypatch.setattr(
        broker_tools,
        "read_local_runtime_db_config",
        lambda: ("/tmp/runtime.db", 1_000),
    )
    monkeypatch.setattr(broker_tools, "execute_broker_tool", fake_execute_broker_tool)
    monkeypatch.setattr(
        broker_tools,
        "get_trace_context",
        lambda: SimpleNamespace(extra={"process_id": BROKER_ACTOR_PROCESS_ID}),
    )

    with pytest.raises(ToolCompletionAuditPersistenceError) as caught:
        await broker_tools.run_broker_tool_wrapper(
            MagicMock(),
            "step-1",
            BASH_TOOL,
            {"command": "pwd", "cwd": "."},
            _state(),
            invocation_id=None,
            tool_request_id="request-1",
            requested_at="2026-04-27T00:00:01Z",
            preflight_only=False,
        )

    assert caught.value.tool_invocation_id == "invocation-1"

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import create_state_token_sink

from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime import (
    subagent_spawn,
    subagent_wait,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution import (
    run_validated_tool_impl,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import (
    CANCEL_SUBAGENT_TOOL_ID,
    SEND_MESSAGE_TO_SUBAGENT_TOOL,
    SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
    SPAWN_SUBAGENT_TOOL,
    SPAWN_SUBAGENT_TOOL_ID,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_REGISTRY,
    WAIT_SUBAGENTS_TOOL,
)
from pantaray_agents.local_runtime.runtime.action_subagent_spawn import (
    ActionSubagentSpawnRequest,
    ActionSubagentSpawnResult,
    ExternalSpawnResourceClaim,
    WorkspaceSpawnResourceClaim,
)
from pantaray_agents.local_runtime.runtime.action_subagent_wait import (
    ActionSubagentWaitSnapshot,
)
from pantaray_agents.schema.action_tool_call import ActionToolCallOrigin
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.profiles.subagent_models import SUBAGENT_MODEL_SETTINGS


def _args(model: str = "gpt-6-luna") -> dict[str, JSONValue]:
    return {
        "model": model,
        "task": "Inspect the assigned boundary",
        "context_refs": ["conversation:step-1"],
        "resource_claims": [
            {
                "kind": "external_resource",
                "root_identity": "workspace",
                "normalized_key": "repository:src",
            }
        ],
    }


# The parent's template and the head fields its first turn froze.
_AGENT = SimpleNamespace(executing_prompt="Context at {current_time}\n{action_history}")


def _state() -> ActionAgentState:
    return cast(
        ActionAgentState,
        {
            "user_id": "user-1",
            "action_id": "action-1",
            "execution_session_id": "session-1",
            "manifest_id": "manifest-1",
            "context": {"executing_head_fields": {"current_time": "T0"}},
        },
    )


def test_spawn_tool_registry_is_config_derived_and_production_activated() -> None:
    schema = SPAWN_SUBAGENT_TOOL.build_validation_input_schema()
    model_schema = schema["properties"]["model"]
    selectors = [setting.selector for setting in SUBAGENT_MODEL_SETTINGS]

    assert TOOL_REGISTRY[SPAWN_SUBAGENT_TOOL_ID] is SPAWN_SUBAGENT_TOOL
    assert SEND_MESSAGE_TO_SUBAGENT_TOOL_ID in TOOL_REGISTRY
    assert TOOL_REGISTRY[WAIT_SUBAGENTS_TOOL.tool_id] is WAIT_SUBAGENTS_TOOL
    message_schema = SEND_MESSAGE_TO_SUBAGENT_TOOL.build_validation_input_schema()
    assert message_schema["properties"]["message_id"]["maxLength"] == 128
    assert message_schema["properties"]["content"]["maxLength"] == 8_000
    assert set(schema["properties"]) == {
        "model",
        "task",
        "context_refs",
        "resource_claims",
    }
    assert model_schema["enum"] == selectors
    assert all(
        setting.recommendation in SPAWN_SUBAGENT_TOOL.prompt_contract.description
        for setting in SUBAGENT_MODEL_SETTINGS
    )
    child_tool_ids = {
        SPAWN_SUBAGENT_TOOL_ID,
        SEND_MESSAGE_TO_SUBAGENT_TOOL_ID,
        WAIT_SUBAGENTS_TOOL.tool_id,
        CANCEL_SUBAGENT_TOOL_ID,
    }
    assert child_tool_ids.issubset(SUPERVISOR_SINGLE_REACT_TOOL_IDS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    tuple(setting.selector for setting in SUBAGENT_MODEL_SETTINGS),
)
async def test_spawn_tool_binds_durable_think_trace_and_configured_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    args = _args(selector)
    if selector == "gpt-5.6-sol":
        args["resource_claims"] = [{"kind": "workspace_path", "path": "src"}]
    captured: list[ActionSubagentSpawnRequest] = []

    def _spawn(**kwargs: object) -> ActionSubagentSpawnResult:
        captured.append(cast(ActionSubagentSpawnRequest, kwargs["request"]))
        return ActionSubagentSpawnResult("child-1", "job-1", True)

    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(subagent_spawn, "spawn_action_subagent", _spawn)
    state = _state()
    origin = ActionToolCallOrigin(
        llm_step_id="durable-supervisor-think", call_id="spawn-call"
    )
    with TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id="parent-job",
        extra={"process_id": "parent-process"},
    ):
        result = await run_validated_tool_impl(
            _AGENT,
            SPAWN_SUBAGENT_TOOL,
            args,
            state,
            sink=create_state_token_sink(state),
            runtime=SimpleNamespace(),
            actor="supervisor",
            step_id="random-tool-execution-step",
            origin=origin,
        )
        for error in (
            subagent_spawn.ActionSubagentSpawnRequestError("payload invalid"),
            subagent_spawn.ActionSubagentResourceClaimConflictError("claims overlap"),
            subagent_spawn.ActionSubagentResourceIdentityError("path outside roots"),
        ):
            monkeypatch.setattr(
                subagent_spawn, "spawn_action_subagent", MagicMock(side_effect=error)
            )
            with pytest.raises(ToolValidationError):
                await subagent_spawn.run_spawn_subagent_tool(
                    cast(Any, _AGENT),
                    step_id="retry",
                    tool_def=SPAWN_SUBAGENT_TOOL,
                    args=args,
                    state=state,
                    actor="supervisor",
                    origin=origin,
                )

    request = captured[0]
    assert request.origin == origin
    assert request.logical_request_id != result.step_id
    assert request.model_selector == selector
    # The child starts from the head the parent froze, not from a fresh read.
    assert request.action_context == "Context at T0\n"
    assert result.output == {"child_process_id": "child-1"}
    claim = request.resource_claims[0]
    if selector == "gpt-5.6-sol":
        assert claim == WorkspaceSpawnResourceClaim(
            "src",
            tmp_path / "local_runtime_workspaces/scratch/user-1/action-1/scratch",
        )
    else:
        assert claim == ExternalSpawnResourceClaim("workspace", "repository:src")


@pytest.mark.asyncio
async def test_spawn_tool_rejects_requests_without_adopted_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _spawn(**_kwargs: object) -> ActionSubagentSpawnResult:
        nonlocal called
        called = True
        return ActionSubagentSpawnResult("child-1", "job-1", True)

    monkeypatch.setattr(subagent_spawn, "spawn_action_subagent", _spawn)
    with pytest.raises(ToolValidationError, match="adopted call origin"):
        await subagent_spawn.run_spawn_subagent_tool(
            cast(Any, _AGENT),
            step_id="execution-step",
            tool_def=SPAWN_SUBAGENT_TOOL,
            args=_args(),
            state=_state(),
            actor="supervisor",
            origin=None,
        )
    assert called is False


@pytest.mark.asyncio
async def test_spawn_tool_rejects_goal_worker_before_runtime_mutation() -> None:
    with pytest.raises(ToolValidationError, match="only available to the Supervisor"):
        await subagent_spawn.run_spawn_subagent_tool(
            cast(Any, _AGENT),
            step_id="execution-step",
            tool_def=SPAWN_SUBAGENT_TOOL,
            args=_args(),
            state=_state(),
            actor="goal_worker",
            origin=None,
        )


@pytest.mark.asyncio
async def test_wait_tool_deadline_returns_named_nonterminal_and_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []
    pending = {"child_process_id": "child-2", "status": "nonterminal"}
    initial = ActionSubagentWaitSnapshot(
        ({"child_process_id": "child-1", "status": "nonterminal"}, pending), ()
    )
    deadline = ActionSubagentWaitSnapshot(
        (
            {"child_process_id": "child-1", "status": "success", "report": "ok"},
            pending,
        ),
        ("child-1",),
    )
    snapshots = iter((initial, deadline))

    def _read(**kwargs: object) -> ActionSubagentWaitSnapshot:
        captured.append(kwargs["request"])
        return next(snapshots)

    sleep = AsyncMock()
    monkeypatch.setenv("LOCAL_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("LOCAL_DB_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setattr(subagent_wait, "read_action_subagent_wait_snapshot", _read)
    monkeypatch.setattr(subagent_wait.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        subagent_wait.asyncio,
        "get_running_loop",
        lambda: SimpleNamespace(time=MagicMock(side_effect=(0, 1, 31))),
    )
    state = _state()
    with TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id="parent-job",
        extra={"process_id": "parent-process"},
    ):
        result = await run_validated_tool_impl(
            MagicMock(),
            WAIT_SUBAGENTS_TOOL,
            {"child_process_ids": ["child-1", "child-2"]},
            state,
            sink=create_state_token_sink(state),
            runtime=SimpleNamespace(),
            actor="supervisor",
            step_id="wait-step",
        )

    assert result.output == {"results": list(deadline.results)}
    assert result.subagent_collection_receipt is not None
    assert result.subagent_collection_receipt.request.child_process_ids == ("child-1",)
    sleep.assert_awaited_once()
    assert len(captured) == 2
    assert captured[0].child_process_ids == ("child-1", "child-2")

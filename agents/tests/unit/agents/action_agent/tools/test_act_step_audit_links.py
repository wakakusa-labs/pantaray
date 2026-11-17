from __future__ import annotations

# pylint: disable=import-error,redefined-outer-name,unused-argument,protected-access
import logging
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    base_state,
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    seed_action_header,
)

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.graph import (
    ActionGraphRuntime,
    RuntimeServices,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import action_step
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalRequiredToolControl,
    CompletedToolControl,
    ToolExecutionResult,
    ToolValidationError,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    build_next_action,
    build_tool_call,
)
from pantaray_agents.agents.action_agent.runtime.steps.approval_pause import (
    APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_CODE,
)
from pantaray_agents.agents.action_agent.runtime.steps.persistence import (
    ActionStepPersistenceError,
)
from pantaray_agents.agents.action_agent.runtime.steps.tool import (
    build_finalized_tool_step,
    record_tool_step,
)
from pantaray_agents.agents.action_agent.tools import THINKING_TOOL
from pantaray_agents.agents.core import LlmUsage, TokenBudgetExceeded
from pantaray_agents.application.action.resume_service import (
    ActionResumeService,
    ResumeDeps,
)
from pantaray_agents.local_runtime.agent_state import LocalActionRepository
from pantaray_agents.local_runtime.tooling.models import StoredApprovalSession
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import AgentError
from pantaray_agents.schema.repositories.repository import (
    RepositoryErrorKind,
    RepositoryResult,
)


def _runtime_services(action_agent: ActionAgent) -> RuntimeServices:
    return action_agent._runtime_services  # noqa: SLF001


def _capture_saved_steps(
    action_agent: ActionAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    saved_steps: list[dict[str, object]] = []
    original_save_action_step = action_agent.repository.save_action_step

    async def capture_save_action_step(*args, **kwargs):
        saved_steps.append(dict(kwargs))
        return await original_save_action_step(*args, **kwargs)

    monkeypatch.setattr(
        action_agent.repository,
        "save_action_step",
        capture_save_action_step,
    )
    return saved_steps


def _runtime(
    action_agent: ActionAgent, *, token_budget: int | None
) -> ActionGraphRuntime:
    return ActionGraphRuntime(
        agent=action_agent,
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
        ),
        state_config={
            "max_steps": 5,
            "token_budget": token_budget,
            "prompt_name": "action/executing",
            "prompt_version": "1.0",
        },
        emit_action_step=AsyncMock(),
        emit_error=AsyncMock(),
        services=_runtime_services(action_agent),
    )


async def _thinking_tool_state(
    action_agent: ActionAgent,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ActionAgentState:
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=THINKING_TOOL.tool_id,
            args={"query": "audit link"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )
    return state


@pytest.mark.asyncio
async def test_action_step_links_single_invocation_on_success(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    attempts = {"count": 0}
    step_ids: list[str] = []

    async def successful_run_tool(*_args, **kwargs):
        attempts["count"] += 1
        step_ids.append(kwargs["step_id"])
        return ToolExecutionResult(
            step_id="formal-success-step",
            tool_id=THINKING_TOOL.tool_id,
            status="success",
            started_at="2026-03-26T10:00:00Z",
            completed_at="2026-03-26T10:00:01Z",
            finalized_output=FinalizedToolOutput(
                output={"text": "done"},
                storage_kind="inline_json",
                owner_kind="tool_invocation",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
            ),
            control=CompletedToolControl(),
            tool_invocation_id="invoke-success",
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        successful_run_tool,
    )
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = await _thinking_tool_state(action_agent, monkeypatch, tmp_path)

    await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert attempts["count"] == 1
    assert len(set(step_ids)) == 1
    assert saved_steps[-1]["tool_invocation_ids"] == ("invoke-success",)
    assert saved_steps[-1]["retry_count"] == 0
    assert saved_steps[-1]["step_id"] == step_ids[0]


@pytest.mark.asyncio
async def test_action_step_propagates_token_budget_exceeded_from_tool_run(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    async def budget_exceeded_run_tool(*_args, **kwargs):
        kwargs["sink"].record(
            LlmUsage(2, 0),
            stage="tool::thinking",
            may_raise=True,
        )
        raise AssertionError("record must raise after exceeding the budget")

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        budget_exceeded_run_tool,
    )
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = await _thinking_tool_state(action_agent, monkeypatch, tmp_path)
    state["token_budget"] = 1
    state["tokens_used"] = 0

    sink = create_state_token_sink(state)
    with pytest.raises(TokenBudgetExceeded):
        await action_step(
            action_agent,
            state,
            _runtime(action_agent, token_budget=1),
            sink=sink,
        )

    assert sink.state["status"] == "error"
    assert sink.state["errors"][-1]["error_code"] == ("ACTION_TOKEN_BUDGET_EXCEEDED")
    assert saved_steps == []


@pytest.mark.asyncio
async def test_action_step_links_single_invocation_on_failure(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    attempts = {"count": 0}
    execution_step_ids: list[str] = []

    async def failing_run_tool(*_args, **kwargs):
        attempts["count"] += 1
        execution_step_ids.append(kwargs["step_id"])
        error = RuntimeError("temporary failure")
        error.tool_invocation_id = "invoke-failed"
        raise error

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        failing_run_tool,
    )
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = await _thinking_tool_state(action_agent, monkeypatch, tmp_path)

    updated_state = await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert attempts["count"] == 1
    assert updated_state.get("errors")
    assert saved_steps[-1]["tool_invocation_ids"] == ("invoke-failed",)
    assert saved_steps[-1]["retry_count"] == 0
    assert saved_steps[-1]["step_id"] == execution_step_ids[0]


@pytest.mark.asyncio
async def test_action_step_links_validation_invocation_without_reexecution(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    attempts = {"count": 0}

    async def validation_error(*_args, **_kwargs):
        attempts["count"] += 1
        raise ToolValidationError(
            "validation failed",
            tool_invocation_id="invoke-validation",
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        validation_error,
    )
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = await _thinking_tool_state(action_agent, monkeypatch, tmp_path)

    updated_state = await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert attempts["count"] == 1
    assert updated_state.get("errors")
    assert saved_steps[-1]["tool_invocation_ids"] == ("invoke-validation",)


@pytest.mark.asyncio
async def test_action_step_persists_approval_wait_checkpoint(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    async def approval_required_run_tool(*_args, **_kwargs):
        return ToolExecutionResult(
            step_id="approval-wait-step",
            tool_id=THINKING_TOOL.tool_id,
            status="processing",
            started_at="2026-03-26T10:00:00Z",
            completed_at="2026-03-26T10:00:01Z",
            finalized_output=FinalizedToolOutput(
                output={
                    "kind": "approval_required",
                    "approval_session_id": "approval-session-1",
                    "tool_request_id": "tool-request-1",
                    "intent_class": "thinking",
                    "command_summary": {"summary": "Thinking approval"},
                },
                storage_kind="inline_json",
                owner_kind="action_step",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
            ),
            control=ApprovalRequiredToolControl(
                approval_session_id="approval-session-1",
                tool_request_id="tool-request-1",
                intent_class="thinking",
                command_summary={"summary": "Thinking approval"},
            ),
            tool_invocation_id="invoke-approval-wait",
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        approval_required_run_tool,
    )
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = await _thinking_tool_state(action_agent, monkeypatch, tmp_path)

    updated_state = await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert updated_state["pending_approval_request"]["approval_session_id"] == (
        "approval-session-1"
    )
    assert len(saved_steps) == 1
    saved_step = saved_steps[0]
    assert saved_step["step_name"] == f"tool::{THINKING_TOOL.tool_id}"
    assert saved_step["status"] == "processing"
    assert saved_step["tool_output"]["status"] == "processing"
    assert saved_step["tool_invocation_ids"] == ("invoke-approval-wait",)
    checkpoint = saved_step["runtime_state_checkpoint"]
    assert checkpoint["pending_approval_request"]["approval_session_id"] == (
        "approval-session-1"
    )
    assert checkpoint["current_approval_blockers"][0]["tool_request_id"] == (
        "tool-request-1"
    )


@pytest.mark.asyncio
async def test_action_step_fails_action_when_approval_checkpoint_persistence_fails(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    async def approval_required_run_tool(*_args, **_kwargs):
        return ToolExecutionResult(
            step_id="approval-wait-step",
            tool_id=THINKING_TOOL.tool_id,
            status="processing",
            started_at="2026-03-26T10:00:00Z",
            completed_at="2026-03-26T10:00:01Z",
            finalized_output=FinalizedToolOutput(
                output={
                    "kind": "approval_required",
                    "approval_session_id": "approval-session-1",
                    "tool_request_id": "tool-request-1",
                    "intent_class": "thinking",
                    "command_summary": {"summary": "Thinking approval"},
                },
                storage_kind="inline_json",
                owner_kind="action_step",
                search_text=None,
                stdout_text=None,
                stderr_text=None,
            ),
            control=ApprovalRequiredToolControl(
                approval_session_id="approval-session-1",
                tool_request_id="tool-request-1",
                intent_class="thinking",
                command_summary={"summary": "Thinking approval"},
            ),
            tool_invocation_id="invoke-approval-wait",
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        approval_required_run_tool,
    )
    monkeypatch.setattr(
        action_agent.repository,
        "save_action_step",
        AsyncMock(
            return_value=RepositoryResult(
                error="checkpoint write failed",
                error_kind=RepositoryErrorKind.CONSTRAINT,
                retryable=False,
            )
        ),
    )
    state = await _thinking_tool_state(action_agent, monkeypatch, tmp_path)

    updated_state = await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert updated_state["status"] == "error"
    assert "pending_approval_request" not in updated_state
    assert "current_approval_blockers" not in updated_state
    assert updated_state["errors"][-1]["error_code"] == (
        APPROVAL_PAUSE_CHECKPOINT_PERSIST_FAILED_CODE
    )


@pytest.mark.asyncio
async def test_action_step_persists_pre_run_validation_error_without_invocation(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id="unknown_tool",
            args={"query": "audit link"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert updated_state.get("errors")
    assert saved_steps
    assert saved_steps[-1]["status"] == "error"
    assert saved_steps[-1]["step_type"] == "tool_execution"
    assert saved_steps[-1]["tool_invocation_ids"] == ()


@pytest.mark.asyncio
async def test_pre_run_validation_retry_exceeded_checkpoint_is_terminal(
    action_agent: ActionAgent, monkeypatch, tmp_path
) -> None:
    saved_steps = _capture_saved_steps(action_agent, monkeypatch)
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(action_agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["context"]["tool_validation_error_streak"] = 4
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id="unknown_tool",
            args={"query": "audit link"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    await seed_action_header(
        action_agent,
        action_id="act-123",
        suggestion_id="sug-123",
        user_id="user-123",
    )

    updated_state = await action_step(
        action_agent,
        state,
        _runtime(action_agent, token_budget=None),
        sink=create_state_token_sink(state),
    )

    assert updated_state.get("status") == "error"
    checkpoint = saved_steps[-1]["runtime_state_checkpoint"]
    assert isinstance(checkpoint, dict)
    assert checkpoint["status"] == "error"
    errors = checkpoint["errors"]
    assert isinstance(errors, list)
    assert errors[0]["error_code"] == "ACTION_TOOL_CALL_INVALID_RETRY_EXCEEDED"


_APPROVAL_SESSION_ID = "approval-session-1"
_TOOL_REQUEST_ID = "tool-request-1"
_STATE_CONFIG = {
    "max_steps": 5,
    "max_tool_steps": 5,
    "token_budget": None,
    "prompt_name": "action/executing",
    "prompt_version": "1.0",
    "cancel_check_max_consecutive_failures": 3,
    "cancel_check_failure_grace_seconds": 60,
}


def _durable_action_agent(db_path: Path) -> ActionAgent:
    return ActionAgent(
        config={
            "supabase_client": object(),
            "llm_client": MockLLMClient(),
            "llm": {},
        },
        repository=LocalActionRepository(db_path=db_path, busy_timeout_ms=1_000),
    )


def _approved_once_session(_user_id: str, _request_id: str) -> StoredApprovalSession:
    return StoredApprovalSession(
        approval_session_id=_APPROVAL_SESSION_ID,
        user_id="user-123",
        action_id="act-123",
        manifest_id="manifest-test-123",
        claimed_at=None,
        tool_invocation_id=None,
        tool_id=THINKING_TOOL.tool_id,
        intent_class="thinking",
        approval_source="prompt",
        review_reason=None,
        tool_request_id=_TOOL_REQUEST_ID,
        status="approved_once",
        approved_capabilities_json={},
        command_summary_json={"summary": "Thinking approval"},
        decided_at="2026-03-26T10:00:05Z",
    )


async def _paused_approval_result(*_args, **_kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        step_id="ignored-by-act-step",
        tool_id=THINKING_TOOL.tool_id,
        status="processing",
        started_at="2026-03-26T10:00:00Z",
        completed_at="2026-03-26T10:00:01Z",
        finalized_output=FinalizedToolOutput(
            output={
                "kind": "approval_required",
                "approval_session_id": _APPROVAL_SESSION_ID,
                "tool_request_id": _TOOL_REQUEST_ID,
                "intent_class": "thinking",
                "command_summary": {"summary": "Thinking approval"},
            },
            storage_kind="inline_json",
            owner_kind="action_step",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
        ),
        control=ApprovalRequiredToolControl(
            approval_session_id=_APPROVAL_SESSION_ID,
            tool_request_id=_TOOL_REQUEST_ID,
            intent_class="thinking",
            command_summary={"summary": "Thinking approval"},
        ),
        tool_invocation_id=None,
    )


def _insert_supervisor_think_row(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO agent_action_steps(
                    step_id, action_id, user_id, step_number, local_step_number,
                    short_step_id, step_type, step_name, status, goal_handle,
                    retry_count, prompt_tokens, completion_tokens,
                    completed_at, created_at
                ) VALUES (
                    'supervisor-think-1', 'act-123', 'user-123', 1, 1,
                    'S-1-THINK', 'llm_output', 'supervisor_think', 'success', 'S',
                    0, 0, 0, '2026-03-26T10:00:00Z', '2026-03-26T10:00:00Z'
                )
                """
            )


async def _approved_execution_result(*_args, **_kwargs) -> ToolExecutionResult:
    return ToolExecutionResult(
        step_id="ignored-by-act-step",
        tool_id=THINKING_TOOL.tool_id,
        status="success",
        started_at="2026-03-26T10:00:06Z",
        completed_at="2026-03-26T10:00:07Z",
        finalized_output=FinalizedToolOutput(
            output={"text": "approved run"},
            storage_kind="inline_json",
            owner_kind="tool_invocation",
            search_text=None,
            stdout_text=None,
            stderr_text=None,
        ),
        control=CompletedToolControl(),
        tool_invocation_id=None,
    )


@pytest.mark.asyncio
async def test_approval_resume_settles_the_paused_tool_step_row(
    monkeypatch, tmp_path
) -> None:
    db_path = tmp_path / "runtime.db"
    agent = _durable_action_agent(db_path)
    state = install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=base_state(agent),
        allowed_tool_ids=(THINKING_TOOL.tool_id,),
    )
    state["phase"] = "executing"
    state["step"] = 1
    state["history_by_scope"] = {
        "S": [
            {
                "step_id": "supervisor-think-1",
                "step_number": 1,
                "phase": "executing",
                "step_type": StepType.LLM_OUTPUT,
                "summary": "decide the next tool",
                "tool_id": None,
                "started_at": "2026-03-26T09:59:59Z",
                "completed_at": "2026-03-26T10:00:00Z",
                "short_step_id": "S-1-THINK",
            }
        ]
    }
    state["next_action"] = build_next_action(
        tool=build_tool_call(
            tool_id=THINKING_TOOL.tool_id,
            args={"query": "audit link"},
        ),
        decided_at="2026-03-26T10:00:00Z",
    )
    _insert_supervisor_think_row(db_path)

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        _paused_approval_result,
    )
    paused_state = await action_step(
        agent,
        state,
        _runtime(agent, token_budget=None),
        sink=create_state_token_sink(state),
    )
    assert paused_state["pending_approval_request"] is not None

    checkpoint_row = await agent.repository.get_runtime_checkpoint_for_approval_resume(
        user_id="user-123",
        action_id="act-123",
        approval_session_id=_APPROVAL_SESSION_ID,
        tool_request_id=_TOOL_REQUEST_ID,
    )
    paused_step_id = checkpoint_row.metadata["approval_anchor_step_id"]
    resume_service = ActionResumeService(
        ResumeDeps(
            logger=logging.getLogger(__name__),
            load_approval_session_by_request=_approved_once_session,
            build_agent_error=lambda **values: AgentError(severity="error", **values),
            now_provider=lambda: "2026-03-26T10:00:06Z",
        )
    )
    resumed_state = resume_service.resolve_initial_state(
        request=build_action_request(
            action_id="act-123",
            suggestion_id="sug-123",
            user_id="user-123",
            approval_resume_session_id=_APPROVAL_SESSION_ID,
            approval_resume_tool_request_id=_TOOL_REQUEST_ID,
        ),
        started_at="2026-03-26T10:00:06Z",
        state_config=_STATE_CONFIG,
        token_budget=None,
        checkpoint_row=checkpoint_row,
    )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.call_execution.run_tool",
        _approved_execution_result,
    )
    await action_step(
        agent,
        resumed_state,
        _runtime(agent, token_budget=None),
        sink=create_state_token_sink(resumed_state),
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT step_id, status, short_step_id
            FROM agent_action_steps
            WHERE action_id = 'act-123' AND step_type = 'tool_execution'
            """
        ).fetchall()

    assert rows == [(paused_step_id, "success", "S-1-TOOL")]


@pytest.mark.asyncio
async def test_tool_step_persistence_raises_on_repository_error(
    action_agent: ActionAgent, monkeypatch
) -> None:
    async def failing_save_action_step(*_args, **_kwargs):
        return RepositoryResult(
            error="link failed",
            error_kind=RepositoryErrorKind.CONSTRAINT,
            retryable=False,
        )

    monkeypatch.setattr(
        action_agent.repository,
        "save_action_step",
        failing_save_action_step,
    )
    state = base_state(action_agent)

    with pytest.raises(ActionStepPersistenceError):
        await record_tool_step(
            action_agent,
            state,
            step_id="formal-tool-step",
            action_id=state["action_id"],
            step_number=1,
            step_name=f"tool::{THINKING_TOOL.tool_id}",
            tool_args={
                "tool_id": THINKING_TOOL.tool_id,
                "args": {"query": "audit link"},
            },
            result=build_finalized_tool_step(
                status="success",
                output=FinalizedToolOutput(
                    output={"text": "done"},
                    storage_kind="inline_json",
                    owner_kind="tool_invocation",
                    search_text=None,
                    stdout_text=None,
                    stderr_text=None,
                ),
            ),
            started_at="2026-03-26T10:00:00Z",
            completed_at="2026-03-26T10:00:01Z",
            retry_count=0,
            parent_step_id=None,
            goal_handle="G1",
            user_id=str(state.get("user_id") or ""),
            short_step_id="G1-1-TOOL",
            local_step_number=1,
            tool_invocation_ids=("invoke-1",),
        )

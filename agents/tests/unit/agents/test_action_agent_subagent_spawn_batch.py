"""実際の採用・逐次実行・SQLite保存でspawn要求の再開を検証する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import (
    attach_local_execution_context,
    build_action_request,
    create_state_token_sink,
    install_local_runtime_env,
)
from tests.unit.local_runtime import test_action_subagent_spawn as spawn

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act.step import (
    action_step,
)
from pantaray_agents.agents.action_agent.runtime.state import create_initial_state
from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.local_runtime.agent_state import LocalActionRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.schema.repositories.repository import RepositoryResult
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_agents.utils.trace_context import TraceContextManager
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse, LlmCommentary
from pantaray_llm.contracts.tool_use import LlmToolCall


class _ProcessCrash(BaseException):
    """プロセス消失は通常のツール失敗として保存されない。"""


async def _noop(_event: object) -> None:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["complete", "post_commit_crash", "legacy_checkpoint"]
)
async def test_adopted_spawn_calls_execute_and_resume_with_their_original_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Literal["complete", "post_commit_crash", "legacy_checkpoint"],
) -> None:
    db_path, context = spawn._runtime(tmp_path, seed_think=False)
    install_local_runtime_env(monkeypatch=monkeypatch, db_path=db_path)
    repository = LocalActionRepository(db_path=db_path, busy_timeout_ms=1_000)
    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        return_value=PromptConfig(prompt="{current_time}", system_instruction="SYS"),
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=repository,
        )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id=None,
        action_id="action-1",
        started_at=spawn.TIMESTAMP,
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    attach_local_execution_context(
        state,
        manifest_id=context.manifest_id,
        execution_session_id=context.execution_session_id,
        execution_network_policy=context.network_policy,
        read_access_scope=context.read_access_scope,
        action_temp_dir=str(context.action_temp_dir),
        app_runtime_python=str(context.app_runtime_python),
    )
    state["phase"] = "executing"
    state["step"] = 2
    state["context"]["local_step_counters"] = {"S": 1}
    # What the THINK that requested the spawns froze as its head.
    state["context"]["executing_head_fields"] = {"current_time": "T0"}
    state["history_by_scope"]["S"] = [
        {
            "step_id": "root-user-step",
            "step_number": 1,
            "phase": "init",
            "step_type": "user_request",
            "summary": "",
            "user_request_text": "Start work",
            "tool_id": None,
            "started_at": spawn.TIMESTAMP,
            "completed_at": spawn.TIMESTAMP,
            "short_step_id": "S-1-USER",
        }
    ]
    request = build_action_request(
        action_id="action-1", suggestion_id=None, user_id="user-1"
    )
    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 20,
            "max_tool_steps": 20,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
            "cancel_check_max_consecutive_failures": 3,
            "cancel_check_failure_grace_seconds": 60,
        },
        emit_action_step=_noop,
        emit_error=_noop,
        services=agent._runtime_services,
    )
    state = agent.resume_service.apply_latest_runtime_state_config(
        checkpoint_state=state, state_config=runtime.state_config
    )
    calls = [
        LlmToolCall(
            call_id="read-plan",
            name="read_action_plan",
            arguments={STEP_NOTE_ARG: "既存の作業計画を確認する。"},
        ),
        *(
            LlmToolCall(
                call_id=f"spawn-{index}",
                name="spawn_subagent",
                arguments={
                    STEP_NOTE_ARG: "分担した範囲の調査を開始する。",
                    "model": "gpt-6-luna",
                    "task": f"Inspect scope {index}",
                    "context_refs": ["S-1-USER"],
                    "resource_claims": [
                        {
                            "kind": "external_resource",
                            "root_identity": "workspace",
                            "normalized_key": f"scope-{index}",
                        }
                    ],
                },
            )
            for index in (1, 2)
        ),
    ]
    if scenario == "legacy_checkpoint":
        calls = calls[1:2]
    agent._generate_llm_action_turn = AsyncMock(
        return_value=LlmActionTurnResponse(
            mode="action_turn",
            messages=[
                LlmCommentary(
                    phase="commentary",
                    source_message_id="spawn-plan",
                    text="計画を確認し、独立した二つの範囲を調べます。",
                )
            ],
            calls=calls,
        )
    )
    save_step = repository.save_action_step

    async def crash_before_tool_result_save(**values: object):
        if values["step_name"] == "tool::spawn_subagent":
            raise _ProcessCrash
        return await save_step(**values)

    with TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id="parent-job",
        extra={"process_id": "parent-process"},
    ):
        state = await execution_think_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )
        batch = state["next_action"].batch
        assert [call.tool_id for call in batch.calls] == [call.name for call in calls]
        assert batch.mode == "sequential"
        if scenario == "legacy_checkpoint":
            checkpoint = build_runtime_state_checkpoint(state)
            del checkpoint["next_action"]["batch"]
            state = agent.resume_service.resolve_initial_state(
                request=request,
                started_at=spawn.TIMESTAMP,
                state_config=runtime.state_config,
                token_budget=None,
                checkpoint_row=RepositoryResult(
                    data={
                        "runtime_state_checkpoint_version": RUNTIME_STATE_CHECKPOINT_VERSION,
                        "runtime_state_checkpoint": checkpoint,
                    }
                ),
            )
            state = await action_step(
                agent, state, runtime, sink=create_state_token_sink(state)
            )
            assert state["status"] == "error"
            assert state["phase"] == "finalizing"
            assert (
                state["errors"][0]["error_code"] == "ACTION_RESUME_CHECKPOINT_INVALID"
            )
            assert spawn._row_counts(db_path) == (0, 0, 0)
            return
        origins = [call.origin for call in batch.calls[1:]]
        if scenario == "post_commit_crash":
            monkeypatch.setattr(
                repository, "save_action_step", crash_before_tool_result_save
            )
            with pytest.raises(_ProcessCrash):
                await action_step(
                    agent, state, runtime, sink=create_state_token_sink(state)
                )
            assert spawn._row_counts(db_path) == (1, 1, 1)
            with spawn._connect(db_path) as connection:
                checkpoint = connection.execute(
                    "SELECT runtime_state_checkpoint FROM agent_action_steps "
                    "WHERE step_name='tool::read_action_plan'"
                ).fetchone()[0]
            state = restore_runtime_state_checkpoint(
                json.loads(checkpoint),
                expected_action_id=request.action_id,
                expected_suggestion_id=request.suggestion_id,
                expected_user_id=request.user_id,
            )
            state = agent.resume_service.apply_latest_runtime_state_config(
                checkpoint_state=state, state_config=runtime.state_config
            )
            assert [call.origin for call in state["next_action"].batch.calls] == origins
            monkeypatch.setattr(repository, "save_action_step", save_step)
        state = await action_step(
            agent, state, runtime, sink=create_state_token_sink(state)
        )

    assert state["status"] == "processing"
    assert state["next_action"] is None
    assert spawn._row_counts(db_path) == (2, 2, 2)
    with spawn._connect(db_path) as connection:
        steps = connection.execute(
            "SELECT step_name, status FROM agent_action_steps "
            "WHERE step_type='tool_execution' ORDER BY step_number"
        ).fetchall()
        payloads = connection.execute(
            "SELECT payload_json FROM job_payloads "
            "JOIN jobs USING(job_id) WHERE job_type='execute_action_subagent'"
        ).fetchall()
    assert [tuple(row) for row in steps] == [
        ("tool::read_action_plan", "success"),
        ("tool::spawn_subagent", "success"),
        ("tool::spawn_subagent", "success"),
    ]
    assert sorted(json.loads(row[0])["task"] for row in payloads) == [
        "Inspect scope 1",
        "Inspect scope 2",
    ]
    assert all(
        json.loads(row[0])["inference_profile_id"] == "action.subagent.luna"
        and json.loads(row[0])["action_context"] == "T0"
        for row in payloads
    )

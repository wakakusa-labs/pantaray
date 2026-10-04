"""バッチ内で承認が必要になったときの all-or-nothing pause と再開の契約テスト。"""

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.agents.action_agent.fixtures import (
    build_action_request,
    create_state_token_sink,
    install_local_runtime_tool_context,
    project_request_user_step,
)

from pantaray_agents.agents.action_agent import ActionAgent
from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.nodes import (
    execution_think_step,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act import (
    call_execution,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.act import step as act
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ApprovalDeniedToolControl,
    ApprovalRequiredToolControl,
    CompletedToolControl,
    ToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.tools import STEP_NOTE_ARG
from pantaray_agents.application.action.approval_resume_restoration import (
    normalize_approval_resume_state,
)
from pantaray_agents.application.action.resume_service import (
    _RESTORED_OPTIONAL_STATE_FIELDS,
)
from pantaray_agents.local_runtime.tooling.models import StoredApprovalSession
from pantaray_agents.local_runtime.tooling.tool_result_finalization import (
    FinalizedToolOutput,
)
from pantaray_agents.mock.mock_agent_repository import MockActionAgentRepository
from pantaray_agents.mock.mock_llm_client import MockLLMClient
from pantaray_agents.mock.mock_repository import MockRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import JSONValue
from pantaray_agents.utils.prompt_loader import PromptConfig
from pantaray_llm.contracts.action_turn import LlmActionTurnResponse
from pantaray_llm.contracts.tool_use import LlmToolCall

PATCH_TOOL = "apply_patch"
_APPROVAL_SESSION_ID = "approval-1"
_TOOL_REQUEST_ID = "request-1"


def _patch_args(index: int) -> dict[str, JSONValue]:
    return {
        "changes": [
            {
                "kind": "update_file",
                "path": f"notes-{index}.txt",
                "unified_diff": "@@ -1 +1 @@\n-a\n+b\n",
            }
        ]
    }


@pytest.fixture(autouse=True)
def _clear_mock_repo_data() -> None:
    MockRepository.clear_data()


def _note(index: int) -> str:
    return f"{index} 件目の呼び出しとして、必要な変更をここで適用する。"


def _turn(count: int) -> LlmActionTurnResponse:
    return LlmActionTurnResponse(
        mode="action_turn",
        messages=[],
        calls=[
            LlmToolCall(
                call_id=f"call-{index}",
                name=PATCH_TOOL,
                arguments={**_patch_args(index), STEP_NOTE_ARG: _note(index)},
            )
            for index in range(count)
        ],
    )


def _make_agent() -> ActionAgent:
    def _fake_load_config(_prompt_name: str) -> PromptConfig:
        return PromptConfig(prompt="{current_time}", system_instruction="SYS")

    with patch(
        "pantaray_agents.agents.core.base.prompt_loader.load_config",
        side_effect=_fake_load_config,
    ):
        agent = ActionAgent(
            config={"llm_client": MockLLMClient(), "llm": {}},
            repository=MockActionAgentRepository(),
        )
    agent._cancellation_service.check_cancellation = AsyncMock(  # type: ignore[attr-defined]
        return_value=False
    )
    agent._consume_llm_thoughts = MagicMock(return_value=None)  # type: ignore[attr-defined]
    return agent


async def _build_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path, *, action_id: str):
    agent = _make_agent()

    async def noop(_event):  # type: ignore[no-untyped-def]
        return None

    request = build_action_request(
        action_id=action_id,
        suggestion_id=f"sug-{action_id}",
        user_id=f"user-{action_id}",
    )
    runtime = ActionGraphRuntime(
        agent=agent,
        request=request,
        state_config={
            "max_steps": 20,
            "token_budget": None,
            "prompt_name": "action/executing",
            "prompt_version": "test",
        },
        emit_action_step=noop,
        emit_error=AsyncMock(),
        services=agent._runtime_services,  # noqa: SLF001
    )
    state = create_initial_state(
        user_id=request.user_id,
        suggestion_id=request.suggestion_id,
        action_id=request.action_id,
        started_at=datetime.now(UTC).isoformat(),
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state = project_request_user_step(state, request)
    state["phase"] = "executing"
    install_local_runtime_tool_context(
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        state=state,
        allowed_tool_ids=(PATCH_TOOL,),
    )
    await agent.repository.save_action(
        {
            "action_id": request.action_id,
            "suggestion_id": request.suggestion_id,
            "user_id": request.user_id,
            "final_output": "",
            "status": "processing",
            "prompt_name": "action/executing",
            "prompt_version": "test",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
        prompt_name="action/executing",
        prompt_version="test",
    )
    return agent, runtime, state, request


async def _think(agent, runtime, state: ActionAgentState) -> ActionAgentState:
    return await execution_think_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )


async def _act(agent, runtime, state: ActionAgentState) -> ActionAgentState:
    return await act.action_step(
        agent, state, runtime, sink=create_state_token_sink(state)
    )


def _persisted_tool_steps(agent, action_id: str) -> list[dict[str, Any]]:
    return sorted(
        (
            step
            for step in agent.repository.data["action_steps"]
            if step.get("action_id") == action_id
            and str(step.get("step_name") or "").startswith("tool::")
        ),
        key=lambda step: step["step_number"],
    )


def _distinct_persisted_step_ids(agent, action_id: str) -> int:
    """論理的な TOOL 行数。mock リポジトリは step_id を upsert せず追記する。"""

    return len({step["step_id"] for step in _persisted_tool_steps(agent, action_id)})


def _tool_history(state: ActionAgentState) -> list[dict[str, Any]]:
    return [
        entry
        for entry in state["history_by_scope"]["S"]
        if entry["step_type"] == StepType.TOOL_EXECUTION
    ]


def _finalized(output: dict[str, JSONValue]) -> FinalizedToolOutput:
    return FinalizedToolOutput(
        output=output,
        storage_kind="inline_json",
        owner_kind="tool_invocation",
        search_text=None,
        stdout_text=None,
        stderr_text=None,
    )


def _success_result(*, step_id: str, tool_id: str) -> ToolExecutionResult:
    now = datetime.now(UTC).isoformat()
    return ToolExecutionResult(
        step_id=step_id,
        tool_id=tool_id,
        status="success",
        started_at=now,
        completed_at=now,
        finalized_output=_finalized({"tool_id": tool_id}),
        control=CompletedToolControl(),
    )


def _approval_required_result(*, step_id: str, tool_id: str) -> ToolExecutionResult:
    now = datetime.now(UTC).isoformat()
    return ToolExecutionResult(
        step_id=step_id,
        tool_id=tool_id,
        status="processing",
        started_at=now,
        completed_at=now,
        finalized_output=_finalized({"kind": "approval_required"}),
        control=ApprovalRequiredToolControl(
            approval_session_id=_APPROVAL_SESSION_ID,
            tool_request_id=_TOOL_REQUEST_ID,
            intent_class="workspace_write",
            command_summary={"kind": "apply_patch"},
        ),
    )


def _denied_result(*, step_id: str, tool_id: str) -> ToolExecutionResult:
    now = datetime.now(UTC).isoformat()
    return ToolExecutionResult(
        step_id=step_id,
        tool_id=tool_id,
        status="success",
        started_at=now,
        completed_at=now,
        finalized_output=_finalized({"kind": "approval_denied", "executed": False}),
        control=ApprovalDeniedToolControl(
            approval_session_id=_APPROVAL_SESSION_ID,
            tool_request_id=_TOOL_REQUEST_ID,
        ),
    )


def _stored_session(*, request, status: str) -> StoredApprovalSession:
    return StoredApprovalSession(
        approval_session_id=_APPROVAL_SESSION_ID,
        user_id=request.user_id,
        action_id=request.action_id,
        manifest_id="manifest-test-123",
        claimed_at=None,
        tool_invocation_id=None,
        tool_id=PATCH_TOOL,
        intent_class="workspace_write",
        approval_source="prompt",
        tool_request_id=_TOOL_REQUEST_ID,
        status=cast(Any, status),
        approved_capabilities_json={},
        command_summary_json={"kind": "apply_patch"},
        decided_at=datetime.now(UTC).isoformat(),
    )


def _restore(checkpoint, request) -> ActionAgentState:
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id=request.action_id,
        expected_suggestion_id=request.suggestion_id,
        expected_user_id=request.user_id,
    )
    # resume_service.apply_latest_runtime_state_config と同じ復元後の正規化。
    mutable = cast("MutableMapping[str, object]", restored)
    for field_name, default_value in _RESTORED_OPTIONAL_STATE_FIELDS:
        mutable.setdefault(field_name, default_value)
    restored["token_budget"] = None
    return restored


def _resume_from_pause(
    paused_state: ActionAgentState,
    *,
    request,
    agent,
    decision: str,
) -> ActionAgentState:
    """pause 行の checkpoint を復元し、承認・拒否の決定を適用する。"""

    pause_row = _persisted_tool_steps(agent, request.action_id)[-1]
    restored = _restore(pause_row["runtime_state_checkpoint"], request)
    return normalize_approval_resume_state(
        restored,
        load_approval_session_by_request=lambda _user_id, _tool_request_id: (
            _stored_session(request=request, status=decision)
        ),
        now_provider=lambda: datetime.now(UTC).isoformat(),
        paused_tool_step_id=str(pause_row["step_id"]),
    )


async def _run_batch_pausing_on_the_second_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, action_id: str
):
    agent, runtime, state, request = await _build_fixture(
        monkeypatch, tmp_path, action_id=action_id
    )
    agent._generate_llm_action_turn = AsyncMock(return_value=_turn(3))  # type: ignore[attr-defined]

    state = await _think(agent, runtime, state)
    approval_step_number = state["step"] + 1
    started_step_numbers: list[int] = []

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        started_step_numbers.append(kwargs["step_number"])
        if kwargs["step_number"] == approval_step_number:
            return _approval_required_result(
                step_id=kwargs["step_id"], tool_id=tool_def.tool_id
            )
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)
    state = await _act(agent, runtime, state)
    return agent, runtime, state, request, started_step_numbers, approval_step_number


@pytest.mark.asyncio
async def test_approval_inside_a_batch_pauses_and_starts_no_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """K=3 の 2 件目が承認待ちになったら、3 件目は開始せず pause 行 1 件で止まる。"""

    (
        agent,
        _runtime,
        state,
        request,
        started,
        approval_step_number,
    ) = await _run_batch_pausing_on_the_second_call(
        monkeypatch, tmp_path, action_id="act-batch-pause"
    )

    assert started == [approval_step_number - 1, approval_step_number]
    persisted = _persisted_tool_steps(agent, request.action_id)
    assert [step["status"] for step in persisted] == ["success", "processing"]
    assert state["pending_approval_request"] is not None
    assert state["status"] == "processing"
    # pause した呼び出しは logical step を消費していない。
    assert state["step"] == approval_step_number

    # 未実行の 2 件（承認待ち + その後ろ）だけが checkpoint に残る。
    remaining = state["next_action"].batch
    assert [call.step_note for call in remaining.calls] == [_note(1), _note(2)]
    assert state["next_action"].tool == remaining.calls[0].call

    restored = _restore(persisted[-1]["runtime_state_checkpoint"], request)
    assert [call.step_note for call in restored["next_action"].batch.calls] == [
        _note(1),
        _note(2),
    ]
    assert restored["pending_approval_request"].tool_call.args == _patch_args(1)


@pytest.mark.asyncio
async def test_approved_resume_runs_the_approved_call_then_the_remainder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """承認後の再開は、承認された呼び出しを同じ行へ決着させ、残りを続けて実行する。"""

    (
        agent,
        runtime,
        state,
        request,
        _started,
        approval_step_number,
    ) = await _run_batch_pausing_on_the_second_call(
        monkeypatch, tmp_path, action_id="act-batch-approved"
    )
    pause_short_step_id = _tool_history(state)[-1]["short_step_id"]
    paused_origins = [call.origin for call in state["next_action"].batch.calls]

    resumed = _resume_from_pause(
        state, request=request, agent=agent, decision="approved_once"
    )
    assert resumed.get("pending_approval_request") is None
    assert [
        call.origin for call in resumed["next_action"].batch.calls
    ] == paused_origins
    assert [call.step_note for call in resumed["next_action"].batch.calls] == [
        _note(1),
        _note(2),
    ]

    executed: list[int] = []

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        executed.append(kwargs["step_number"])
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)
    resumed = await _act(agent, runtime, resumed)

    assert executed == [approval_step_number, approval_step_number + 1]
    assert resumed["next_action"] is None
    tool_entries = _tool_history(resumed)
    assert [entry["step_number"] for entry in tool_entries] == [
        approval_step_number - 1,
        approval_step_number,
        approval_step_number + 1,
    ]
    assert len({entry["short_step_id"] for entry in tool_entries}) == 3
    # 承認待ちだった行はフォークせず、その場で ok に決着する。
    settled = tool_entries[1]
    assert settled["short_step_id"] == pause_short_step_id
    assert str(settled["result_line"]).startswith(f"{PATCH_TOOL}: ok")
    assert _distinct_persisted_step_ids(agent, request.action_id) == 3


@pytest.mark.asyncio
async def test_denied_resume_records_the_denial_then_runs_the_remainder(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """拒否されたら、その呼び出しを拒否として残したうえで残りを実行する。"""

    (
        agent,
        runtime,
        state,
        request,
        _started,
        approval_step_number,
    ) = await _run_batch_pausing_on_the_second_call(
        monkeypatch, tmp_path, action_id="act-batch-denied"
    )

    resumed = _resume_from_pause(state, request=request, agent=agent, decision="denied")

    executed: list[int] = []

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        executed.append(kwargs["step_number"])
        if kwargs["step_number"] == approval_step_number:
            return _denied_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)
    resumed = await _act(agent, runtime, resumed)

    assert executed == [approval_step_number, approval_step_number + 1]
    assert resumed["status"] == "processing"
    denied_entry = _tool_history(resumed)[1]
    assert denied_entry["output"]["kind"] == "approval_denied"
    assert denied_entry["output"]["executed"] is False
    assert len(_tool_history(resumed)) == 3
    assert _distinct_persisted_step_ids(agent, request.action_id) == 3


@pytest.mark.asyncio
async def test_cancel_between_pause_and_resume_leaves_the_remainder_unstarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """pause 中に停止要求が届いたら、再開しても残りは 1 件も開始しない。"""

    (
        agent,
        runtime,
        state,
        request,
        _started,
        _approval_step_number,
    ) = await _run_batch_pausing_on_the_second_call(
        monkeypatch, tmp_path, action_id="act-batch-pause-cancel"
    )

    resumed = _resume_from_pause(
        state, request=request, agent=agent, decision="approved_once"
    )

    async def _cancel(checked_state: ActionAgentState) -> bool:
        checked_state["status"] = "canceled"
        checked_state["next_action"] = None
        return True

    agent._cancellation_service.check_cancellation = _cancel  # type: ignore[attr-defined]

    executed: list[int] = []

    async def _fake_run_tool(_agent, tool_def, _args, _state, **kwargs):
        executed.append(kwargs["step_number"])
        return _success_result(step_id=kwargs["step_id"], tool_id=tool_def.tool_id)

    monkeypatch.setattr(call_execution, "run_tool", _fake_run_tool)
    resumed = await _act(agent, runtime, resumed)

    assert executed == []
    assert resumed["status"] == "canceled"
    assert len(_persisted_tool_steps(agent, request.action_id)) == 2

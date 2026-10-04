from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
from tests.unit.agents.action_agent.fixtures import (
    create_state,
    install_local_runtime_env,
)
from tests.unit.local_runtime.test_action_final_gate import _collect
from tests.unit.local_runtime.test_action_parent_child_settlement import (
    BUSY_TIMEOUT_MS,
    PARENT_JOB_ID,
    PARENT_PROCESS_ID,
    _claim_child,
    _fence_parent,
    _parent_runtime,
    _pause_child,
    _seed_child,
)

from pantaray_agents.agents.action_agent.runtime.conversation_service import (
    append_goal_message,
)
from pantaray_agents.agents.action_agent.runtime.graph import ActionGraphRuntime
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.execution import (
    run_validated_tool_impl,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.memory_links import (
    run_get_memory_reference_tool,
)
from pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime.shared import (
    ToolValidationError,
    UnprojectedToolExecutionResult,
)
from pantaray_agents.agents.action_agent.runtime.models.conversation import (
    GoalConversationStateModel,
)
from pantaray_agents.agents.action_agent.runtime.state import ActionAgentState
from pantaray_agents.agents.action_agent.tools import (
    DRAFT_FINAL_ANSWER_TOOL,
    GET_MEMORY_REFERENCE_TOOL,
    SUBMIT_FINAL_ANSWER_TOOL,
    SUPERVISOR_SINGLE_REACT_TOOL_IDS,
    TOOL_REGISTRY,
)
from pantaray_agents.agents.core import CountingSink
from pantaray_agents.local_runtime.memory_catalog.checkpoint import (
    deserialize_memory_draft,
    serialize_memory_draft,
    serialize_memory_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.draft import create_memory_draft
from pantaray_agents.local_runtime.memory_catalog.epoch import (
    build_memory_context_epoch,
)
from pantaray_agents.local_runtime.memory_catalog.errors import (
    MemoryReferenceDepthError,
    MemoryReferenceInputError,
)
from pantaray_agents.local_runtime.memory_catalog.models import (
    MemoryDocument,
    MemoryFragment,
    MemoryNode,
)
from pantaray_agents.local_runtime.runtime.action_final_gate import (
    ActionFinalizationAuthorityError,
)
from pantaray_agents.local_runtime.runtime.action_subagent_terminal import (
    build_action_subagent_failure_result,
    finalize_action_subagent_terminal,
)
from pantaray_agents.utils.trace_context import TraceContextManager


def _preparing_action_node() -> MemoryNode:
    return MemoryNode(
        user_id="user-1",
        node_id="memory-action-1",
        source="action",
        source_record_id="action-1",
        lifecycle="preparing",
        integrity="healthy",
        current_revision_id=None,
    )


@pytest.mark.asyncio
async def test_unknown_local_ref_error_does_not_change_action_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = create_state()
    source_node = MemoryNode(
        user_id=state["user_id"],
        node_id="memory-source-1",
        source="activity_log",
        source_record_id="log-1",
        lifecycle="active",
        integrity="healthy",
        current_revision_id="revision-1",
    )
    source_fragment = MemoryFragment(
        user_id=state["user_id"],
        fragment_id="fragment-1",
        revision_id="revision-1",
        source_path="body.md",
        block_kind="paragraph",
        block_index=1,
        heading_path=None,
        content_text="Visible context without the requested ref.",
        content_sha256="sha256",
    )
    epoch = build_memory_context_epoch(
        run_id="run-1",
        user_id=state["user_id"],
        visible=((source_node, source_fragment, "visible context"),),
    )
    state["memory_context_epoch"] = serialize_memory_epoch(epoch)

    def reject_unknown_ref(**_kwargs: object) -> NoReturn:
        raise MemoryReferenceInputError(
            "visible source fragment does not contain the ref"
        )

    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_links.read_local_runtime_db_config",
        lambda: (tmp_path / "runtime.db", 1_000),
    )
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_links.follow_memory_reference",
        reject_unknown_ref,
    )

    with pytest.raises(MemoryReferenceInputError):
        await run_get_memory_reference_tool(
            step_id="step-ref",
            tool_def=GET_MEMORY_REFERENCE_TOOL,
            args={
                "source_handle": epoch.items[0].item.context_handle,
                "local_ref_id": "ref_missing",
            },
            state=state,
        )

    assert state["memory_context_epoch"] == serialize_memory_epoch(epoch)


@pytest.mark.asyncio
async def test_action_reference_tool_rejects_second_hop_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = create_state()
    source_node = MemoryNode(
        user_id=state["user_id"],
        node_id="memory-source-1",
        source="fact",
        source_record_id="fact-1",
        lifecycle="active",
        integrity="healthy",
        current_revision_id="revision-1",
    )
    source_fragment = MemoryFragment(
        user_id=state["user_id"],
        fragment_id="fragment-1",
        revision_id="revision-1",
        source_path="facts.md",
        block_kind="paragraph",
        block_index=1,
        heading_path=None,
        content_text="Reference-derived context.",
        content_sha256="sha256",
    )
    root_epoch = build_memory_context_epoch(
        run_id="run-1",
        user_id=state["user_id"],
        visible=((source_node, source_fragment, "followed reference"),),
    )
    reference_epoch = replace(
        root_epoch,
        items=(replace(root_epoch.items[0], reference_depth=1),),
    )
    state["memory_context_epoch"] = serialize_memory_epoch(reference_epoch)
    read_config = MagicMock()
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "memory_links.read_local_runtime_db_config",
        read_config,
    )

    with pytest.raises(MemoryReferenceDepthError):
        await run_get_memory_reference_tool(
            step_id="step-ref",
            tool_def=GET_MEMORY_REFERENCE_TOOL,
            args={
                "source_handle": reference_epoch.items[0].item.context_handle,
                "local_ref_id": "ref_2",
            },
            state=state,
        )

    read_config.assert_not_called()


@pytest.mark.asyncio
async def test_supervisor_draft_final_answer_stores_pending_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = create_state()
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "draft_final_answer.ensure_preparing_node",
        lambda **_kwargs: _preparing_action_node(),
    )

    result = await run_validated_tool_impl(
        MagicMock(),
        DRAFT_FINAL_ANSWER_TOOL,
        {"answer": " draft v1 "},
        state,
        sink=CountingSink(),
        runtime=SimpleNamespace(),
        actor="supervisor",
        step_id="step-draft",
    )

    assert result.status == "success"
    assert state["supervisor_pending_final_answer"] == "draft v1"
    # link_memory checks this value against the stored draft before editing it.
    assert isinstance(result.output, dict)
    assert result.output["draft_revision"] == _stored_draft_revision(state)


@pytest.mark.asyncio
async def test_supervisor_draft_final_answer_replaces_pending_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = create_state()
    state["supervisor_pending_final_answer"] = "old draft"
    monkeypatch.setattr(
        "pantaray_agents.agents.action_agent.runtime.handlers.tool_runtime."
        "draft_final_answer.ensure_preparing_node",
        lambda **_kwargs: _preparing_action_node(),
    )

    first = await run_validated_tool_impl(
        MagicMock(),
        DRAFT_FINAL_ANSWER_TOOL,
        {"answer": "first draft"},
        state,
        sink=CountingSink(),
        runtime=SimpleNamespace(),
        actor="supervisor",
        step_id="step-draft-1",
    )
    second = await run_validated_tool_impl(
        MagicMock(),
        DRAFT_FINAL_ANSWER_TOOL,
        {"answer": "new draft"},
        state,
        sink=CountingSink(),
        runtime=SimpleNamespace(),
        actor="supervisor",
        step_id="step-draft",
    )

    assert state["supervisor_pending_final_answer"] == "new draft"
    assert isinstance(first.output, dict) and isinstance(second.output, dict)
    assert second.output["draft_revision"] == _stored_draft_revision(state)
    assert second.output["draft_revision"] != first.output["draft_revision"]


def _stored_draft_revision(state: ActionAgentState) -> str:
    return deserialize_memory_draft(state["supervisor_memory_draft"]).draft_revision


def _drafted_state() -> ActionAgentState:
    """A Supervisor state whose only remaining question is the ownership gate."""

    state = create_state(action_id="action-1")
    state["user_id"] = "user-1"
    state["supervisor_pending_final_answer"] = "Final answer"
    state["supervisor_memory_draft"] = serialize_memory_draft(
        create_memory_draft(
            user_id="user-1",
            owner_node_id="memory-action-1",
            base_revision_id=None,
            documents=(MemoryDocument("body.md", "Final answer"),),
        )
    )
    return state


async def _submit(state: ActionAgentState) -> UnprojectedToolExecutionResult:
    with TraceContextManager(
        user_id="user-1",
        action_id="action-1",
        local_job_id=PARENT_JOB_ID,
        extra={"process_id": PARENT_PROCESS_ID},
    ):
        return await run_validated_tool_impl(
            MagicMock(),
            SUBMIT_FINAL_ANSWER_TOOL,
            {},
            state,
            sink=CountingSink(),
            runtime=SimpleNamespace(),
            actor="supervisor",
            step_id="step-submit",
        )


@pytest.mark.asyncio
async def test_submit_final_answer_commits_supervisor_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_local_runtime_env(
        monkeypatch=monkeypatch, db_path=_parent_runtime(tmp_path)
    )
    state = _drafted_state()

    result = await _submit(state)

    assert result.status == "success"
    assert result.output == {"status": "final_answer_submitted"}
    assert state["final_output"] == "Final answer"
    assert state["status"] == "success"
    assert state["next_action"] is None
    assert "supervisor_pending_final_answer" not in state


@pytest.mark.asyncio
async def test_submit_final_answer_names_the_remedy_for_every_unsettled_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = install_local_runtime_env(
        monkeypatch=monkeypatch, db_path=_parent_runtime(tmp_path)
    )
    child = _seed_child(db_path)
    _claim_child(db_path)
    session_id = _pause_child(db_path, child)
    state = _drafted_state()

    with pytest.raises(ToolValidationError, match="unsettled work") as exc_info:
        await _submit(state)

    assert exc_info.value.details == {
        "rule": "FINALIZATION_BLOCKED",
        "live_child_process_ids": [child["process_id"]],
        "uncollected_child_process_ids": [],
        "pending_approval_session_ids": [session_id],
        "active_claim_ids": ["claim-1"],
    }
    message = str(exc_info.value)
    assert "call wait_subagents for live_child_process_ids" in message
    assert "approves or denies pending_approval_session_ids" in message
    assert "settle the child holding each of active_claim_ids" in message
    assert "uncollected_child_process_ids" not in message
    assert state["status"] == "processing"
    assert state["supervisor_pending_final_answer"] == "Final answer"


@pytest.mark.asyncio
async def test_submit_final_answer_finalizes_over_a_collected_failed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed child is collectible work, not a permanent finalization block.

    The Supervisor decides semantically whether to retry, replace, disclose, or
    finish, so the gate asks only that the child settled and was collected.
    """

    db_path = install_local_runtime_env(
        monkeypatch=monkeypatch, db_path=_parent_runtime(tmp_path)
    )
    child = _seed_child(db_path)
    _claim_child(db_path)
    finalize_action_subagent_terminal(
        db_path=db_path,
        busy_timeout_ms=BUSY_TIMEOUT_MS,
        payload=child,
        result=build_action_subagent_failure_result("child_tool_failed"),
    )

    with pytest.raises(ToolValidationError) as exc_info:
        await _submit(_drafted_state())

    assert exc_info.value.details is not None
    assert exc_info.value.details["uncollected_child_process_ids"] == [
        child["process_id"]
    ]
    assert "uncollected_child_process_ids to collect" in str(exc_info.value)

    _collect(db_path, child["process_id"])
    state = _drafted_state()

    result = await _submit(state)

    assert result.status == "success"
    assert state["final_output"] == "Final answer"


@pytest.mark.asyncio
async def test_submit_final_answer_lets_a_stop_fence_end_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fenced parent must converge on cancellation, not retry the tool.

    Reporting lost authority as a validation error would invite retries until
    the validation streak aborts the Action.
    """

    db_path = install_local_runtime_env(
        monkeypatch=monkeypatch, db_path=_parent_runtime(tmp_path)
    )
    _fence_parent(db_path)

    with pytest.raises(ActionFinalizationAuthorityError):
        await _submit(_drafted_state())


@pytest.mark.asyncio
async def test_submit_final_answer_rejects_missing_draft() -> None:
    state = create_state()

    with pytest.raises(ToolValidationError, match="requires a pending"):
        await run_validated_tool_impl(
            MagicMock(),
            SUBMIT_FINAL_ANSWER_TOOL,
            {},
            state,
            sink=CountingSink(),
            runtime=SimpleNamespace(),
            actor="supervisor",
            step_id="step-submit",
        )


@pytest.mark.asyncio
async def test_submit_final_answer_rejects_goal_worker_phase() -> None:
    state = create_state()
    state["supervisor_pending_final_answer"] = "Final answer"

    with pytest.raises(ToolValidationError, match="only available to the Supervisor"):
        await run_validated_tool_impl(
            MagicMock(),
            SUBMIT_FINAL_ANSWER_TOOL,
            {},
            state,
            sink=CountingSink(),
            runtime=SimpleNamespace(),
            actor="goal_worker",
            step_id="step-submit",
        )


def test_supervisor_tool_registries_include_final_answer_tools() -> None:
    assert "draft_final_answer" in TOOL_REGISTRY
    assert "submit_final_answer" in TOOL_REGISTRY
    assert "draft_final_answer" in SUPERVISOR_SINGLE_REACT_TOOL_IDS
    assert "submit_final_answer" in SUPERVISOR_SINGLE_REACT_TOOL_IDS


@pytest.mark.asyncio
async def test_submit_final_answer_requires_settled_goal_conversations() -> None:
    state = _drafted_state()
    state["goal_conversations"] = {
        "G1": append_goal_message(
            GoalConversationStateModel(goal_id="G1"),
            message_id="m1",
            direction="worker_to_supervisor",
            content="未確認の報告",
            created_at="2026-07-18T00:00:00+00:00",
        )
    }

    with pytest.raises(ToolValidationError, match="Goal Worker") as exc_info:
        await run_validated_tool_impl(
            MagicMock(),
            SUBMIT_FINAL_ANSWER_TOOL,
            {},
            state,
            sink=CountingSink(),
            runtime=SimpleNamespace(),
            actor="supervisor",
            step_id="step-submit",
        )

    assert exc_info.value.details == {
        "path": ["state", "goal_conversations"],
        "active_goals": [],
        "pending_completion_goals": [],
        "unread_worker_message_goals": ["G1"],
    }


def test_route_after_submit_terminal_state_goes_to_finalize() -> None:
    state = create_state()
    state["status"] = "success"
    state["final_output"] = "done"

    route = ActionGraphRuntime.route_after_action(MagicMock(), state)

    assert route == "finalize"

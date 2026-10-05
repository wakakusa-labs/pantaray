from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

from pantaray_agents.agents.action_agent.runtime.checkpoint import (
    RUNTIME_STATE_CHECKPOINT_VERSION,
    build_runtime_state_checkpoint,
    restore_runtime_state_checkpoint,
)
from pantaray_agents.agents.action_agent.runtime.handlers.nodes.llm.recorder import (
    record_llm_step,
)
from pantaray_agents.agents.action_agent.runtime.models import (
    ExecutionContextModel,
    ResumeFailureException,
    ResumeFailureModel,
)
from pantaray_agents.agents.action_agent.runtime.models.checkpoint import (
    HistoryEntryModel,
)
from pantaray_agents.agents.action_agent.runtime.models.conversation import (
    GoalConversationStateModel,
)
from pantaray_agents.agents.action_agent.runtime.state import (
    ActionAgentState,
    ActionPhase,
    create_initial_state,
)
from pantaray_agents.agents.action_agent.runtime.steps.counters import (
    CounterInvariantError,
)
from pantaray_agents.mock.mock_action_agent_repository import MockActionAgentRepository
from pantaray_agents.schema.agent.action import StepType
from pantaray_agents.schema.agent.base import AgentError, JSONValue


@pytest.mark.parametrize(
    ("step_type", "assistant_phase"),
    [
        (StepType.ASSISTANT_MESSAGE, "final_answer"),
        (StepType.LLM_OUTPUT, "commentary"),
    ],
)
def test_checkpoint_rejects_phase_outside_commentary_assistant_history(
    step_type: StepType, assistant_phase: str
) -> None:
    payload = {
        "step_id": "message-1",
        "step_number": 1,
        "phase": "executing",
        "step_type": step_type,
        "summary": "",
        "tool_id": None,
        "started_at": "2026-09-12T00:00:00Z",
        "completed_at": "2026-09-12T00:00:00Z",
        "assistant_phase": assistant_phase,
    }
    if step_type is StepType.ASSISTANT_MESSAGE:
        payload.update(
            assistant_message_text="調査を進めます。", short_step_id="S-1-ASSISTANT"
        )
    with pytest.raises(ValidationError):
        HistoryEntryModel.model_validate(payload)


def test_runtime_checkpoint_restores_retired_screen_capture_context_keys() -> None:
    """退役した screen capture キーを持つ既存 checkpoint も resume できる。"""

    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    legacy_context = cast("dict[str, JSONValue]", checkpoint["context"])
    legacy_context["screen_captures"] = []
    legacy_context["capture_timestamps_formatted"] = "Screenshot metadata"

    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert restored["context"]["screen_captures"] == []  # type: ignore[typeddict-item]


def test_runtime_checkpoint_restores_stored_memory_artifact_references() -> None:
    """Checkpoints stored with the retired path list still resume, unchanged."""

    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["memory_artifact_references"] = []

    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert restored["action_id"] == "action-1"
    assert checkpoint["memory_artifact_references"] == []


def test_runtime_checkpoint_v4_roundtrip_preserves_strict_context_extensions() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["goal_conversations"] = {"G1": GoalConversationStateModel(goal_id="G1")}
    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert RUNTIME_STATE_CHECKPOINT_VERSION == 4
    assert restored["goal_conversations"]["G1"].goal_id == "G1"


def test_runtime_checkpoint_roundtrip_preserves_state() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["step"] = 3
    state["steps_taken"] = 7
    state["llm_steps_taken"] = 4
    state["tool_steps_taken"] = 3
    state["total_prompt_tokens"] = 120
    state["total_completion_tokens"] = 45
    state["tokens_used"] = 165
    state["chunks_sent"] = 2
    state["final_output"] = "final answer"
    state["errors"] = [
        {
            "error_type": "internal_error",
            "error_code": "ACTION_WARNING_SAMPLE",
            "error_message": "sample",
            "severity": "warning",
        }
    ]
    state["history_by_scope"] = {
        "S": [
            {
                "step_id": "step-1",
                "step_number": 2,
                "phase": "executing",
                "step_type": "llm_output",
                "summary": "Selecting next tool",
                "tool_id": "memory_search",
                "started_at": "2026-03-20T10:00:01Z",
                "completed_at": "2026-03-20T10:00:02Z",
                "short_step_id": "S-1-THINK",
            }
        ]
    }
    state["context"]["local_step_counters"] = {"S": 1}

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert restored["action_id"] == state["action_id"]
    assert restored["suggestion_id"] == state["suggestion_id"]
    assert restored["user_id"] == state["user_id"]
    assert restored["phase"] == state["phase"]
    assert restored["phase"] == "executing"
    assert restored["history_by_scope"]["S"][0]["phase"] == "executing"
    assert restored["history_by_scope"]["S"][0]["step_type"] == "llm_output"
    assert restored["step"] == state["step"]
    assert restored["steps_taken"] == state["steps_taken"]
    assert restored["llm_steps_taken"] == state["llm_steps_taken"]
    assert restored["tool_steps_taken"] == state["tool_steps_taken"]
    assert restored["total_prompt_tokens"] == state["total_prompt_tokens"]
    assert restored["total_completion_tokens"] == state["total_completion_tokens"]
    assert restored["tokens_used"] == state["tokens_used"]
    assert checkpoint["chunks_sent"] == state["chunks_sent"]
    assert restored["chunks_sent"] == state["chunks_sent"]
    assert restored["status"] == state["status"]
    assert restored["context"] == state["context"]
    assert restored["history_by_scope"] == state["history_by_scope"]
    assert restored["started_at"] == state["started_at"]
    assert restored["updated_at"] == state["updated_at"]
    assert restored["errors"] == [
        AgentError.model_validate(error).model_dump(mode="json")
        for error in state["errors"]
    ]
    assert restored["final_output"] == state["final_output"]
    checkpoint["step"] = 99
    assert restored["step"] == 3


def test_runtime_checkpoint_roundtrip_preserves_json_null_tool_output() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["history_by_scope"] = {
        "S": [
            {
                "step_id": "step-1",
                "step_number": 1,
                "phase": "executing",
                "step_type": "tool_execution",
                "summary": "Tool returned JSON null",
                "tool_id": "nullable_tool",
                "started_at": "2026-03-20T10:00:01Z",
                "completed_at": "2026-03-20T10:00:02Z",
                "output": None,
            }
        ]
    }

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert "output" in checkpoint["history_by_scope"]["S"][0]
    assert checkpoint["history_by_scope"]["S"][0]["output"] is None
    assert "output" in restored["history_by_scope"]["S"][0]
    assert restored["history_by_scope"]["S"][0]["output"] is None


def test_runtime_checkpoint_roundtrip_preserves_user_request_history() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = "planning"
    state["step"] = 2
    state["context"]["local_step_counters"] = {"S": 1}
    state["history_by_scope"]["S"] = [
        {
            "step_id": "step-user-1",
            "step_number": 1,
            "phase": "init",
            "step_type": "user_request",
            "summary": "",
            "user_request_text": "Build history_fetch",
            "tool_id": None,
            "started_at": "2026-03-20T10:00:00Z",
            "completed_at": "2026-03-20T10:00:00Z",
            "short_step_id": "S-1-USER",
        }
    ]

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert restored["history_by_scope"]["S"] == state["history_by_scope"]["S"]


def _user_image_history_state(attachment: dict[str, object]) -> ActionAgentState:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["context"]["local_step_counters"] = {"S": 1}
    state["step"] = 2
    state["history_by_scope"]["S"] = [
        {
            "step_id": "step-user-1",
            "step_number": 1,
            "phase": "init",
            "step_type": "user_request",
            "summary": "",
            "user_request_text": "Look at this",
            "tool_id": None,
            "started_at": "2026-03-20T10:00:00Z",
            "completed_at": "2026-03-20T10:00:00Z",
            "short_step_id": "S-1-USER",
            "attachments": [attachment],
        }
    ]
    return state


def test_runtime_checkpoint_roundtrip_preserves_user_image_attachment() -> None:
    state = _user_image_history_state(
        {
            "type": "file",
            "source_kind": "local_image_blob",
            "ref": "user_attachment:" + "b" * 24,
            "blob_ref": "attachment_blob_" + "b" * 24,
            "display_path": "image-1.png",
            "storage_path": "user-1/2026-03-20/"
            "5f1c2f7c-6a2f-4d5e-9c1a-0b2c3d4e5f60.png",
            "mime_type": "image/png",
            "byte_size": 12,
            "sha256": "b" * 64,
        }
    )

    restored = restore_runtime_state_checkpoint(
        build_runtime_state_checkpoint(state),
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert restored["history_by_scope"]["S"] == state["history_by_scope"]["S"]


def test_runtime_checkpoint_rejects_tool_attachment_on_user_request_history() -> None:
    state = _user_image_history_state(
        {
            "type": "file",
            "source_kind": "workspace_file",
            "ref": "tool_attachment:workspace",
            "blob_ref": "attachment_blob_workspace",
            "display_path": "/workspace/document.pdf",
            "workspace_root_path": "/workspace",
            "workspace_relative_path": "document.pdf",
            "mime_type": "application/pdf",
            "byte_size": 12,
            "sha256": "a" * 64,
        }
    )

    with pytest.raises(ValueError, match="only local image attachments"):
        build_runtime_state_checkpoint(state)


def test_runtime_checkpoint_roundtrip_preserves_workspace_file_attachment() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["history_by_scope"] = {
        "S": [
            {
                "step_id": "step-1",
                "step_number": 1,
                "phase": "executing",
                "step_type": "tool_execution",
                "summary": "Read workspace PDF",
                "tool_id": "read",
                "started_at": "2026-03-20T10:00:01Z",
                "completed_at": "2026-03-20T10:00:02Z",
                "attachments": [
                    {
                        "type": "file",
                        "source_kind": "workspace_file",
                        "ref": "tool_attachment:workspace",
                        "blob_ref": "attachment_blob_workspace",
                        "display_path": "/workspace/document.pdf",
                        "workspace_root_path": "/workspace",
                        "workspace_relative_path": "document.pdf",
                        "mime_type": "application/pdf",
                        "byte_size": 12,
                        "sha256": "a" * 64,
                    }
                ],
                "short_step_id": "S-1-TOOL",
            }
        ]
    }

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    restored_attachment = restored["history_by_scope"]["S"][0]["attachments"][0]
    assert restored_attachment["source_kind"] == "workspace_file"
    assert restored_attachment["workspace_root_path"] == "/workspace"
    assert restored_attachment["workspace_relative_path"] == "document.pdf"
    assert restored_attachment["sha256"] == "a" * 64


def test_runtime_checkpoint_roundtrip_preserves_supervisor_pending_draft() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["supervisor_pending_final_answer"] = "draft answer"

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert checkpoint["supervisor_pending_final_answer"] == "draft answer"
    assert restored["supervisor_pending_final_answer"] == "draft answer"


@pytest.mark.parametrize("phase", ("planning", "executing", "finalizing"))
def test_runtime_checkpoint_roundtrip_preserves_lifecycle_phase(
    phase: ActionPhase,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = phase

    checkpoint = build_runtime_state_checkpoint(state)
    restored = restore_runtime_state_checkpoint(
        checkpoint,
        expected_action_id="action-1",
        expected_suggestion_id="suggestion-1",
        expected_user_id="user-1",
    )

    assert checkpoint["phase"] == phase
    assert restored["phase"] == phase


@pytest.mark.parametrize("legacy_phase", ("initial_thinking", "meta_think", "act"))
def test_runtime_checkpoint_rejects_legacy_node_phase(
    legacy_phase: str,
) -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["phase"] = legacy_phase

    with pytest.raises(ResumeFailureException) as exc_info:
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )

    assert exc_info.value.failure_code == "ACTION_RESUME_CHECKPOINT_INVALID"


def _checkpoint_with_valid_history_entry() -> dict[str, object]:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = "executing"
    state["history_by_scope"]["S"] = [
        {
            "step_id": "step-1",
            "step_number": 1,
            "phase": "executing",
            "step_type": "llm_output",
            "summary": "Selecting next tool",
            "tool_id": "memory_search",
            "started_at": "2026-03-20T10:00:01Z",
            "completed_at": "2026-03-20T10:00:02Z",
            "short_step_id": "S-1-THINK",
        }
    ]
    return build_runtime_state_checkpoint(state)


def test_runtime_checkpoint_rejects_history_entry_without_step_type() -> None:
    checkpoint = _checkpoint_with_valid_history_entry()
    history_by_scope = checkpoint["history_by_scope"]
    assert isinstance(history_by_scope, dict)
    history = history_by_scope["S"]
    assert isinstance(history, list)
    entry = history[0]
    assert isinstance(entry, dict)
    entry.pop("step_type")

    with pytest.raises(ResumeFailureException) as exc_info:
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )

    assert exc_info.value.failure_code == "ACTION_RESUME_CHECKPOINT_INVALID"


@pytest.mark.parametrize("legacy_phase", ("meta_think", "act"))
def test_runtime_checkpoint_rejects_legacy_history_entry_phase(
    legacy_phase: str,
) -> None:
    checkpoint = _checkpoint_with_valid_history_entry()
    history_by_scope = checkpoint["history_by_scope"]
    assert isinstance(history_by_scope, dict)
    history = history_by_scope["S"]
    assert isinstance(history, list)
    entry = history[0]
    assert isinstance(entry, dict)
    entry["phase"] = legacy_phase

    with pytest.raises(ResumeFailureException) as exc_info:
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )

    assert exc_info.value.failure_code == "ACTION_RESUME_CHECKPOINT_INVALID"


def test_restore_runtime_state_checkpoint_rejects_non_object_next_action() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["next_action"] = "malformed"

    with pytest.raises(
        ValueError,
        match="next_action",
    ):
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )


def test_restore_runtime_state_checkpoint_rejects_non_string_goal_worker_current_goal_id() -> (
    None
):
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["goal_worker_current_goal_id"] = {"bad": "value"}
    checkpoint["goal_worker_phase"] = "running"

    with pytest.raises(
        ValueError,
        match="goal_worker_current_goal_id",
    ):
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )


def test_restore_runtime_state_checkpoint_classifies_blank_goal_worker_current_goal_id() -> (
    None
):
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["goal_worker_current_goal_id"] = "   "
    checkpoint["goal_worker_phase"] = "running"

    with pytest.raises(ResumeFailureException) as exc_info:
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )

    assert exc_info.value.failure_code == "ACTION_RESUME_GOAL_WORKER_STATE_INVALID"


def test_build_runtime_state_checkpoint_rejects_missing_required_runtime_field() -> (
    None
):
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state.pop("run_authority")

    with pytest.raises(ValueError, match="run_authority"):
        build_runtime_state_checkpoint(state)


def test_build_runtime_state_checkpoint_rejects_type_coercion() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["steps_taken"] = "7"

    with pytest.raises(ValueError, match="invalid runtime checkpoint state"):
        build_runtime_state_checkpoint(state)


def test_restore_runtime_state_checkpoint_rejects_missing_chunks_sent() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint.pop("chunks_sent")

    with pytest.raises(ResumeFailureException) as exc_info:
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )

    assert exc_info.value.failure_code == "ACTION_RESUME_CHECKPOINT_INVALID"


def test_runtime_checkpoint_execution_context_requires_all_fields() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["manifest_id"] = "manifest-1"

    with pytest.raises(ValueError, match="execution context"):
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )


def test_execution_context_model_requires_complete_context() -> None:
    with pytest.raises(ValueError):
        ExecutionContextModel.from_optional_values(
            manifest_id="manifest-1",
            execution_session_id=None,
            execution_network_policy="restricted",
            action_temp_dir="/tmp/action",
            app_runtime_python="/usr/bin/python3",
            read_access_scope="workspace",
        )


def test_resume_failure_model_builds_canonical_payload() -> None:
    failure = ResumeFailureModel.from_failure_code("ACTION_RESUME_CHECKPOINT_INVALID")

    assert failure.failure_code == "ACTION_RESUME_CHECKPOINT_INVALID"
    assert failure.failure_stage == "resume_failed"
    assert failure.failure_message_public == "Action execution failed."


def test_restore_runtime_state_checkpoint_rejects_unexpected_extra_field() -> None:
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    checkpoint = build_runtime_state_checkpoint(state)
    checkpoint["unexpected_extra"] = "nope"

    with pytest.raises(ValueError, match="unexpected_extra"):
        restore_runtime_state_checkpoint(
            checkpoint,
            expected_action_id="action-1",
            expected_suggestion_id="suggestion-1",
            expected_user_id="user-1",
        )


@pytest.mark.asyncio
async def test_record_llm_step_persists_checkpoint_with_new_history_entry() -> None:
    repository = MockActionAgentRepository()
    await repository.upsert_action_header(
        action_id="action-1",
        user_id="user-1",
        suggestion_id="suggestion-1",
        prompt_name="action/executing",
        prompt_version="1.0",
    )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = "executing"
    agent = SimpleNamespace(repository=repository)

    await record_llm_step(
        agent,  # type: ignore[arg-type]
        state,
        scope_handle="S",
        step_id="step-1",
        step_number=1,
        step_name="supervisor_think",
        phase="executing",
        summary="Checked the request; searching memory for prior context.",
        llm_prompt_text="prompt",
        llm_response_text='{"tool_id":"memory_search","args":{"query":"foo"}}',
        status="success",
        started_at="2026-03-20T10:00:01Z",
        completed_at="2026-03-20T10:00:02Z",
        tool_id="memory_search",
        thinking="hidden thoughts",
        args={"query": "foo"},
        short_step_id="S-1-THINK",
        local_step_number=1,
    )

    saved_step = repository.data["action_steps"][0]
    checkpoint = saved_step["runtime_state_checkpoint"]
    assert saved_step["runtime_state_checkpoint_version"] == (
        RUNTIME_STATE_CHECKPOINT_VERSION
    )
    assert checkpoint["history_by_scope"]["S"][0]["step_id"] == "step-1"
    assert checkpoint["history_by_scope"]["S"][0]["phase"] == "executing"
    assert checkpoint["history_by_scope"]["S"][0]["step_type"] == "llm_output"
    assert state["history_by_scope"]["S"][0]["step_id"] == "step-1"


@pytest.mark.asyncio
async def test_record_llm_step_rejects_non_positive_step_number() -> None:
    repository = MockActionAgentRepository()
    await repository.upsert_action_header(
        action_id="action-1",
        user_id="user-1",
        suggestion_id="suggestion-1",
        prompt_name="action/executing",
        prompt_version="1.0",
    )
    state = create_initial_state(
        user_id="user-1",
        suggestion_id="suggestion-1",
        action_id="action-1",
        started_at="2026-03-20T10:00:00Z",
        max_steps=20,
        max_tool_steps=20,
        token_budget=None,
    )
    state["phase"] = "planning"
    agent = SimpleNamespace(repository=repository)

    with pytest.raises(
        CounterInvariantError,
        match="record_llm_step.step_number must be >= 1",
    ):
        await record_llm_step(
            agent,  # type: ignore[arg-type]
            state,
            scope_handle="S",
            step_id="step-invalid",
            step_number=0,
            step_name="supervisor_think",
            phase="planning",
            summary="Initial thinking completed",
            llm_prompt_text="prompt",
            llm_response_text="response",
            status="success",
            started_at="2026-03-20T10:00:01Z",
            completed_at="2026-03-20T10:00:02Z",
            short_step_id="S-1-THINK",
            local_step_number=1,
        )
